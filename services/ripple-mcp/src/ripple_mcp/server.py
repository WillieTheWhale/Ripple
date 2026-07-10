"""MCP tools exposing RIPPLE ingest and query capabilities."""

from __future__ import annotations

import atexit
import asyncio
import copy
import hashlib
import logging
import os
import re
import signal
import shutil
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Condition, Lock, Thread
from typing import Any, Literal, TypedDict

from mcp.server.fastmcp import FastMCP

from ripple.db import create_driver, read_records
from ripple.chassis_db import DEFAULT_DB_PATH, add_memory_note, list_memory_notes
from ripple.ingest.covers import add_static_covers
from ripple.ingest.extract import extract
from ripple.ingest.mapping import map_graph
from ripple.ingest.risk import apply_risk
from ripple.ingest.writer import IngestWriter, WriterConfig
from ripple.query.blast import blast_radius as core_blast_radius
from ripple.query.blast import resolve_fqn
from ripple.query.graphview import repo_stats as core_repo_stats
from ripple.memory import CogneeClient
from ripple.sandbox import provider_from_env
from ripple.sandbox.base import Sandbox
from ripple.sandbox.local_docker import LocalDockerSandbox
from ripple.sandbox.local_docker import reap_orphaned_local_docker_sandboxes
from ripple.sandbox.verify import scoped_tests as core_scoped_tests
from ripple.sandbox.verify import verify_patch as core_verify_patch

logger = logging.getLogger(__name__)

JobState = Literal["running", "done", "failed"]


class Progress(TypedDict):
    stage: str
    pct: float
    detail: str


class JobRecord(TypedDict, total=False):
    job_id: str
    kind: str
    status: JobState
    progress: Progress
    result: dict[str, Any]
    error: str
    started_at: float
    finished_at: float


@dataclass
class SandboxRecord:
    sandbox_id: str
    repo_id: str
    sandbox: Sandbox
    created_at: float
    touched_at: float
    source_revision: str
    active_operations: int = 0
    operation_lock: Lock = field(default_factory=Lock, repr=False)


_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ripple-job")
_jobs: dict[str, JobRecord] = {}
_jobs_lock = Lock()
_finalized_fixes: dict[str, tuple[float, str, dict[str, Any]]] = {}
_finalized_fixes_lock = Lock()
_finalized_fixes_condition = Condition(_finalized_fixes_lock)
_sandboxes: dict[str, SandboxRecord] = {}
_sandboxes_lock = Lock()
_SANDBOX_IDLE_SECONDS = 15 * 60
_FINALIZED_FIX_TTL_SECONDS = 60 * 60
_FINALIZED_FIX_MAX = 256
_driver = create_driver()
_MCP_HOST = os.environ.get("RIPPLE_MCP_HOST", "127.0.0.1")
_MCP_PORT = int(os.environ.get("RIPPLE_MCP_PORT", "8790"))
_MCP_PATH = os.environ.get("RIPPLE_MCP_PATH", "/mcp")

mcp = FastMCP(
    "ripple",
    instructions="RIPPLE graph ingest, analysis, sandbox verification, and fix-mode tools.",
    host=_MCP_HOST,
    port=_MCP_PORT,
    streamable_http_path=_MCP_PATH,
)


@mcp.tool()
def ingest_repo(repo_url_or_path: str, repo_id: str, wipe: bool = True) -> dict[str, str]:
    """Start a background RIPPLE ingestion job for a local path or git URL."""
    if not repo_url_or_path or not repo_url_or_path.strip():
        raise ValueError("repo_url_or_path is required")
    if not repo_id or not repo_id.strip():
        raise ValueError("repo_id is required")

    job_id = uuid.uuid4().hex
    _set_job(
        job_id,
        status="running",
        started_at=time.time(),
        progress={"stage": "queued", "pct": 0.0, "detail": "waiting for worker"},
    )
    _executor.submit(_run_ingest_job, job_id, repo_url_or_path.strip(), repo_id.strip(), bool(wipe))
    return {"job_id": job_id, "status": "running"}


@mcp.tool()
def job_status(job_id: str) -> dict[str, Any]:
    """Return current status for a background RIPPLE MCP job."""
    with _jobs_lock:
        record = dict(_jobs.get(job_id, {}))
    if not record:
        raise ValueError(f"Unknown job_id: {job_id}")
    return record


@mcp.tool()
def blast_radius(repo_id: str, fqn: str, max_hops: int = 4) -> dict[str, Any]:
    """Return impacted callers for a function plus the exact Cypher used."""
    return asdict(core_blast_radius(_driver, repo_id, fqn, max_hops=max_hops))


@mcp.tool()
def resolve_symbol(repo_id: str, query: str) -> list[str]:
    """Resolve a symbol query to candidate Function FQNs."""
    return resolve_fqn(_driver, repo_id, query)


@mcp.tool()
def repo_stats(repo_id: str) -> dict[str, int | float]:
    """Return aggregate graph stats for an ingested repository."""
    return core_repo_stats(_driver, repo_id)


@mcp.tool()
def memory_recall(repo_id: str, query: str) -> list[dict[str, Any]]:
    """Recall repository-scoped decisions and fragility notes from Cognee."""
    normalized_repo = (repo_id or "").strip()
    if not normalized_repo:
        raise ValueError("repo_id is required")
    if not query or not query.strip():
        raise ValueError("query is required")

    async def recall() -> list[dict[str, Any]]:
        async with CogneeClient.from_env() as client:
            return [dict(item) for item in await client.recall(normalized_repo, query.strip())]

    return asyncio.run(recall())


@mcp.tool()
def memory_remember(repo_id: str, note: str) -> dict[str, Any]:
    """Store a repository decision in Cognee and mirror it for the UI timeline."""
    normalized_repo = (repo_id or "").strip()
    normalized_note = (note or "").strip()
    if not normalized_repo:
        raise ValueError("repo_id is required")
    if not normalized_note:
        raise ValueError("note is required")

    async def remember() -> object | None:
        async with CogneeClient.from_env() as client:
            return (await client.remember(normalized_repo, normalized_note)).response

    response = asyncio.run(remember())
    cognee_ref = _cognee_reference(response)
    db_path = Path(os.environ.get("RIPPLE_CHASSIS_DB_PATH", str(DEFAULT_DB_PATH)))
    mirrored = add_memory_note(normalized_repo, normalized_note, cognee_ref, db_path=db_path)
    return {
        "repo_id": normalized_repo,
        "note": normalized_note,
        "cognee_ref": cognee_ref,
        "mirror_id": mirrored.id,
        "created_at": mirrored.created_at.isoformat(),
    }


@mcp.tool()
def memory_list(repo_id: str) -> list[dict[str, Any]]:
    """List locally mirrored memory notes for a repository timeline."""
    normalized_repo = (repo_id or "").strip()
    if not normalized_repo:
        raise ValueError("repo_id is required")
    db_path = Path(os.environ.get("RIPPLE_CHASSIS_DB_PATH", str(DEFAULT_DB_PATH)))
    return [
        {
            "id": note.id,
            "repo_id": note.repo_id,
            "summary": note.summary,
            "cognee_ref": note.cognee_ref,
            "created_at": note.created_at.isoformat(),
        }
        for note in list_memory_notes(normalized_repo, db_path=db_path)
    ]


@mcp.tool()
def prepare_sandbox(repo_id: str) -> dict[str, str]:
    """Ensure a repository snapshot and create a live sandbox in a background job."""
    if not repo_id or not repo_id.strip():
        raise ValueError("repo_id is required")
    job_id = uuid.uuid4().hex
    _set_job(
        job_id,
        status="running",
        started_at=time.time(),
        progress={"stage": "queued", "pct": 0.0, "detail": "waiting for sandbox worker"},
    )
    _executor.submit(_run_prepare_sandbox_job, job_id, repo_id.strip())
    return {"job_id": job_id, "status": "running"}


@mcp.tool()
def sandbox_run(sandbox_id: str, command: str, cwd: str | None = None) -> dict[str, Any]:
    """Run a short command in a live sandbox."""
    if not command or not command.strip():
        raise ValueError("command is required")
    record = _reserve_sandbox_operation(sandbox_id)
    try:
        with record.operation_lock:
            return dict(record.sandbox.run_command(command, cwd=cwd, timeout=15))
    finally:
        _release_sandbox_operation(record)


@mcp.tool()
def sandbox_upload(sandbox_id: str, path: str, content: str) -> dict[str, bool]:
    """Upload a small text file into a live sandbox."""
    if not path:
        raise ValueError("path is required")
    record = _reserve_sandbox_operation(sandbox_id)
    try:
        with record.operation_lock:
            record.sandbox.upload_file(path, content)
    finally:
        _release_sandbox_operation(record)
    return {"ok": True}


@mcp.tool()
def sandbox_download(sandbox_id: str, path: str) -> dict[str, str]:
    """Download a small text file from a live sandbox."""
    if not path:
        raise ValueError("path is required")
    record = _reserve_sandbox_operation(sandbox_id)
    try:
        with record.operation_lock:
            return {"content": record.sandbox.download_file(path)}
    finally:
        _release_sandbox_operation(record)


@mcp.tool()
def read_function_source(repo_id: str, fqn: str) -> dict[str, Any]:
    """Return location metadata and source text for a function in the original checkout."""
    if not repo_id or not repo_id.strip():
        raise ValueError("repo_id is required")
    if not fqn or not fqn.strip():
        raise ValueError("fqn is required")
    return _read_function_source(repo_id.strip(), fqn.strip())


@mcp.tool()
def scoped_tests(repo_id: str, fqns: list[str]) -> list[str]:
    """Return COVERS-derived test files for impacted FQNs."""
    if not repo_id or not repo_id.strip():
        raise ValueError("repo_id is required")
    return core_scoped_tests(_driver, repo_id.strip(), fqns)


@mcp.tool()
def verify_patch(
    repo_id: str,
    sandbox_id: str,
    request_id: str,
    diff_text: str,
    fqns: list[str],
) -> dict[str, str]:
    """Verify a diff in a sandbox using scoped tests in a background job."""
    if not repo_id or not repo_id.strip():
        raise ValueError("repo_id is required")
    if not diff_text or not diff_text.strip():
        raise ValueError("diff_text is required")
    normalized_request_id = _normalize_request_id(request_id)
    normalized_repo_id = repo_id.strip()
    record = _reserve_sandbox_operation(sandbox_id, repo_id=normalized_repo_id)
    if not record.source_revision:
        _release_sandbox_operation(record)
        raise ValueError(f"Sandbox {sandbox_id} is missing a source revision")
    job_id = uuid.uuid4().hex
    _set_job(
        job_id,
        kind="verify_patch",
        status="running",
        started_at=time.time(),
        progress={"stage": "queued", "pct": 0.0, "detail": "waiting for verification worker"},
        result={"repo_id": normalized_repo_id, "source_revision": record.source_revision},
    )
    try:
        _executor.submit(
            _run_verify_patch_job,
            job_id,
            normalized_repo_id,
            sandbox_id,
            diff_text,
            fqns,
            normalized_request_id,
        )
    except Exception:
        _release_sandbox_operation(record)
        raise
    logger.info(
        "Queued verification request_id=%s job_id=%s repo_id=%s patch_bytes=%s",
        normalized_request_id,
        job_id,
        normalized_repo_id,
        len(diff_text.encode("utf-8")),
    )
    return {"job_id": job_id, "status": "running"}


@mcp.tool()
def verify_fix(
    repo_id: str,
    request_id: str,
    diff_text: str,
    fqns: list[str],
) -> dict[str, str]:
    """Create a disposable sandbox and verify a request-bound draft diff."""
    normalized_repo_id = (repo_id or "").strip()
    if not normalized_repo_id:
        raise ValueError("repo_id is required")
    normalized_request_id = _normalize_request_id(request_id)
    if not diff_text or not diff_text.strip():
        raise ValueError("diff_text is required")
    normalized_fqns = [str(fqn).strip() for fqn in fqns if str(fqn).strip()]
    if not normalized_fqns:
        raise ValueError("fqns must contain at least one production function")

    job_id = uuid.uuid4().hex
    _set_job(
        job_id,
        kind="verify_patch",
        status="running",
        started_at=time.time(),
        progress={"stage": "queued", "pct": 0.0, "detail": "waiting for verification worker"},
        result={"repo_id": normalized_repo_id},
    )
    _executor.submit(
        _run_verify_fix_job,
        job_id,
        normalized_repo_id,
        normalized_request_id,
        diff_text,
        normalized_fqns,
    )
    logger.info(
        "Queued composite verification request_id=%s job_id=%s repo_id=%s",
        normalized_request_id,
        job_id,
        normalized_repo_id,
    )
    return {"job_id": job_id, "status": "running"}


@mcp.tool()
def open_draft_pr(
    repo_id: str,
    verify_job_id: str,
    branch_name: str,
    diff_text: str,
    title: str,
    body: str,
) -> dict[str, Any]:
    """Open a draft PR for a verified diff, or report that PR creation is disabled."""
    if not repo_id or not repo_id.strip():
        raise ValueError("repo_id is required")
    normalized_repo_id = repo_id.strip()
    verified_diff, source_revision = _verified_proof_for_pr(
        normalized_repo_id, verify_job_id, diff_text
    )

    pr_repo = os.environ.get("RIPPLE_PR_REPO", "").strip()
    if not pr_repo:
        return {"skipped": True, "reason": "RIPPLE_PR_REPO is not set"}

    job_id = uuid.uuid4().hex
    _set_job(
        job_id,
        kind="open_draft_pr",
        status="running",
        started_at=time.time(),
        progress={"stage": "queued", "pct": 0.0, "detail": "waiting for PR worker"},
        result={
            "repo_id": normalized_repo_id,
            "verify_job_id": verify_job_id,
            "source_revision": source_revision,
        },
    )
    _executor.submit(
        _run_open_pr_job,
        job_id,
        normalized_repo_id,
        verify_job_id,
        source_revision,
        branch_name,
        verified_diff,
        title,
        body,
        pr_repo,
    )
    return {"job_id": job_id, "status": "running"}


@mcp.tool()
def finalize_fix_result(
    verify_job_id: str,
    pr_job_id: str | None,
    impact: list[dict[str, Any]],
    cypher_used: list[str],
    iterations: int,
    request_id: str | None = None,
    memory_notes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build final fix-mode JSON, resolving a PR URL only from its matching job."""
    with _jobs_lock:
        record = dict(_jobs.get(verify_job_id, {}))
    if not record:
        raise ValueError(f"Unknown verify_job_id: {verify_job_id}")
    if record.get("kind") != "verify_patch":
        raise ValueError(f"Job is not a verification job: {verify_job_id}")
    if record.get("status") != "done":
        raise ValueError(f"Verification job is not done: {verify_job_id}")
    result = record.get("result")
    if not isinstance(result, dict):
        raise ValueError(f"Verification job has no result: {verify_job_id}")

    pr_url = _pr_url_for_finalize(verify_job_id, pr_job_id)

    payload = _build_fix_payload(
        result,
        pr_url,
        impact,
        cypher_used,
        iterations,
        memory_notes=memory_notes,
    )
    if request_id is not None:
        _store_finalized_fix(request_id, verify_job_id, payload)
    logger.info(
        "Finalized fix request_id=%s verify_job_id=%s passed=%s",
        request_id or "none",
        verify_job_id,
        payload["verification"]["passed"],
    )
    return payload


@mcp.tool()
def get_finalized_fix_result(request_id: str, wait_seconds: float = 0) -> dict[str, Any]:
    """Return the exact proof payload previously finalized for a request."""
    normalized = _normalize_request_id(request_id)
    bounded_wait = max(0.0, min(float(wait_seconds), 60.0))
    deadline = time.monotonic() + bounded_wait
    with _finalized_fixes_condition:
        while True:
            _prune_finalized_fixes(time.time())
            record = _finalized_fixes.get(normalized)
            if record is not None:
                return copy.deepcopy(record[2])
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _finalized_fixes_condition.wait(remaining)
    raise ValueError(f"No finalized fix result for request_id: {normalized}")


def main() -> int:
    """Run the MCP server on the Streamable HTTP transport."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    _reap_orphaned_local_sandboxes()
    logger.info("Starting RIPPLE MCP on http://%s:%s%s", _MCP_HOST, _MCP_PORT, _MCP_PATH)
    mcp.run(transport="streamable-http")
    return 0


def _run_ingest_job(job_id: str, repo: str, repo_id: str, wipe: bool) -> None:
    started = time.monotonic()
    try:
        _progress(job_id, "clone", 0.0, "resolving repository")
        repo_path = _prepare_repo(repo, repo_id)
        _progress(job_id, "clone", 1.0, str(repo_path))

        _progress(job_id, "extract", 0.0, "running axon pipeline")
        axon_graph = extract(repo_path)
        _progress(job_id, "extract", 1.0, f"{axon_graph.node_count} nodes")

        _progress(job_id, "map", 0.0, "mapping ripple schema")
        mapped = map_graph(axon_graph, repo_path, repo_id, commits=200)
        _progress(job_id, "map", 1.0, f"{len(mapped.nodes)} nodes")

        _progress(job_id, "covers", 0.0, "static")
        covers = add_static_covers(axon_graph, mapped)
        _progress(job_id, "covers", 1.0, f"{covers} covers")

        _progress(job_id, "risk", 0.0, "computing analytics")
        apply_risk(mapped)
        _progress(job_id, "risk", 1.0, "analytics complete")

        _progress(job_id, "write", 0.0, "writing neo4j")
        writer = IngestWriter(WriterConfig.from_env())
        try:
            writer.write(mapped, wipe=wipe)
        finally:
            writer.close()
        _progress(job_id, "write", 1.0, "write complete")

        result: dict[str, Any] = {
            "stage": "done",
            "repo_id": repo_id,
            "nodes": len(mapped.nodes),
            "edges": len(mapped.relationships),
            "functions": len(mapped.nodes_with_label("Function")),
            "tests": len(mapped.nodes_with_label("Test")),
            "covers": len(mapped.relationships_of_type("COVERS")),
            "duration_s": round(time.monotonic() - started, 3),
        }
        _set_job(
            job_id,
            status="done",
            progress={"stage": "done", "pct": 1.0, "detail": "ingestion complete"},
            result=result,
            finished_at=time.time(),
        )
    except Exception as exc:
        logger.exception("Ingest job %s failed", job_id)
        _set_job(
            job_id,
            status="failed",
            progress={"stage": "failed", "pct": 1.0, "detail": str(exc)},
            error=str(exc),
            finished_at=time.time(),
        )


def _run_prepare_sandbox_job(job_id: str, repo_id: str) -> None:
    try:
        _progress(job_id, "repo", 0.05, "resolving repository root")
        repo_root = _repo_root_for(repo_id)
        source_revision = _source_revision_or_content_identity(repo_root)

        _progress(job_id, "snapshot", 0.15, "ensuring sandbox snapshot")
        provider = provider_from_env()
        snapshot_id = provider.ensure_snapshot(repo_id, str(repo_root))
        if _source_revision_or_content_identity(repo_root) != source_revision:
            raise RuntimeError("Source changed while preparing the sandbox snapshot")

        _progress(job_id, "sandbox", 0.85, "creating sandbox")
        sandbox = provider.create(repo_id)
        sandbox_id = _register_sandbox(repo_id, sandbox, source_revision)

        _set_job(
            job_id,
            status="done",
            progress={"stage": "done", "pct": 1.0, "detail": "sandbox ready"},
            result={
                "repo_id": repo_id,
                "sandbox_id": sandbox_id,
                "snapshot": snapshot_id,
                "source_revision": source_revision,
            },
            finished_at=time.time(),
        )
    except Exception as exc:
        logger.exception("Prepare sandbox job %s failed", job_id)
        _set_job(
            job_id,
            status="failed",
            progress={"stage": "failed", "pct": 1.0, "detail": str(exc)},
            error=str(exc),
            finished_at=time.time(),
        )


def _run_verify_patch_job(
    job_id: str,
    repo_id: str,
    sandbox_id: str,
    diff_text: str,
    fqns: list[str],
    request_id: str | None = None,
) -> None:
    try:
        _progress(job_id, "scope", 0.1, "resolving scoped tests")
        tests = core_scoped_tests(_driver, repo_id, fqns)

        _progress(job_id, "verify", 0.35, "applying patch and running tests")
        record = _get_sandbox_record(sandbox_id, repo_id=repo_id)
        if not record.source_revision:
            raise ValueError(f"Sandbox {sandbox_id} is missing a source revision")
        with record.operation_lock:
            result = core_verify_patch(record.sandbox, diff_text, tests)
            result_data = asdict(result)
            verified_diff = result.diff if result.applied else None
            if not verified_diff:
                verified_diff = diff_text if diff_text.endswith("\n") else f"{diff_text}\n"

            proof_replayed = False
            if result.passed:
                proof_replayed = True
                replay = core_verify_patch(record.sandbox, verified_diff, tests)
                result_data = asdict(replay)
                if replay.passed and replay.diff.rstrip("\n") != verified_diff.rstrip("\n"):
                    result_data["passed"] = False
                    result_data["log_tail"] = "Canonical diff changed during proof replay"

        job_result = {
            **result_data,
            "repo_id": repo_id,
            "source_revision": record.source_revision,
            "scoped_tests": tests,
            "diff": verified_diff,
            "diff_text": verified_diff,
            "proof_replayed": proof_replayed,
        }
        if (
            request_id
            and bool(job_result.get("passed"))
            and not os.environ.get("RIPPLE_PR_REPO", "").strip()
        ):
            impact, cypher_used = _proof_context_for_fqns(repo_id, fqns)
            payload = _build_fix_payload(job_result, None, impact, cypher_used, 1)
            _store_finalized_fix(request_id, job_id, payload)
            logger.info(
                "Published request-bound verified proof request_id=%s verify_job_id=%s",
                request_id,
                job_id,
            )
        _set_job(
            job_id,
            status="done",
            progress={"stage": "done", "pct": 1.0, "detail": "verification complete"},
            result=job_result,
            finished_at=time.time(),
        )
        logger.info(
            "Verification complete request_id=%s job_id=%s applied=%s passed=%s replayed=%s",
            request_id or "none",
            job_id,
            bool(job_result.get("applied")),
            bool(job_result.get("passed")),
            bool(job_result.get("proof_replayed")),
        )
    except Exception as exc:
        logger.exception("Verify patch job %s failed", job_id)
        _set_job(
            job_id,
            status="failed",
            progress={"stage": "failed", "pct": 1.0, "detail": str(exc)},
            error=str(exc),
            finished_at=time.time(),
        )
    finally:
        _release_sandbox_operation_by_id(sandbox_id)


def _run_verify_fix_job(
    job_id: str,
    repo_id: str,
    request_id: str,
    diff_text: str,
    fqns: list[str],
) -> None:
    sandbox: Sandbox | None = None
    try:
        _progress(job_id, "repo", 0.05, "resolving immutable source")
        repo_root = _repo_root_for(repo_id)
        source_revision = _source_revision_or_content_identity(repo_root)

        _progress(job_id, "snapshot", 0.15, "ensuring sandbox snapshot")
        provider = provider_from_env()
        provider.ensure_snapshot(repo_id, str(repo_root))
        if _source_revision_or_content_identity(repo_root) != source_revision:
            raise RuntimeError("Source changed while preparing the verification snapshot")

        _progress(job_id, "sandbox", 0.35, "creating disposable sandbox")
        sandbox = provider.create(repo_id)
        tests = core_scoped_tests(_driver, repo_id, fqns)

        _progress(job_id, "verify", 0.55, "applying patch and running scoped tests")
        result = core_verify_patch(sandbox, diff_text, tests)
        result_data = asdict(result)
        verified_diff = result.diff if result.applied else None
        if not verified_diff:
            verified_diff = diff_text if diff_text.endswith("\n") else f"{diff_text}\n"

        proof_replayed = False
        if result.passed:
            proof_replayed = True
            replay = core_verify_patch(sandbox, verified_diff, tests)
            result_data = asdict(replay)
            if replay.passed and replay.diff.rstrip("\n") != verified_diff.rstrip("\n"):
                result_data["passed"] = False
                result_data["log_tail"] = "Canonical diff changed during proof replay"

        job_result = {
            **result_data,
            "repo_id": repo_id,
            "source_revision": source_revision,
            "scoped_tests": tests,
            "diff": verified_diff,
            "diff_text": verified_diff,
            "proof_replayed": proof_replayed,
        }
        if bool(job_result.get("passed")):
            impact, cypher_used = _proof_context_for_fqns(repo_id, fqns)
            payload = _build_fix_payload(job_result, None, impact, cypher_used, 1)
            _store_finalized_fix(request_id, job_id, payload)

        _set_job(
            job_id,
            status="done",
            progress={"stage": "done", "pct": 1.0, "detail": "verification complete"},
            result=job_result,
            finished_at=time.time(),
        )
        logger.info(
            "Composite verification complete request_id=%s job_id=%s applied=%s passed=%s",
            request_id,
            job_id,
            bool(job_result.get("applied")),
            bool(job_result.get("passed")),
        )
    except Exception as exc:
        logger.exception("Composite verify job %s failed", job_id)
        _set_job(
            job_id,
            status="failed",
            progress={"stage": "failed", "pct": 1.0, "detail": str(exc)},
            error=str(exc),
            finished_at=time.time(),
        )
    finally:
        if sandbox is not None:
            try:
                sandbox.destroy()
            except Exception:
                logger.warning("Failed to destroy composite verification sandbox", exc_info=True)


def _run_open_pr_job(
    job_id: str,
    repo_id: str,
    verify_job_id: str,
    source_revision: str,
    branch_name: str,
    diff_text: str,
    title: str,
    body: str,
    pr_repo: str,
) -> None:
    clone_dir = Path(tempfile.mkdtemp(prefix="ripple-pr-"))
    token = os.environ.get("GH_TOKEN") or os.environ.get("ROCKETRIDE_GITHUB_TOKEN")
    try:
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", pr_repo):
            raise ValueError("RIPPLE_PR_REPO must be owner/name")
        if not token:
            raise RuntimeError("GH_TOKEN or ROCKETRIDE_GITHUB_TOKEN is required to open a PR")

        _validated_pr_provenance(job_id, repo_id, verify_job_id, source_revision, diff_text)
        root = _repo_root_for(repo_id)
        _require_clean_git_checkout(root)
        _require_exact_git_revision(root, source_revision)
        branch = _normalize_pr_branch(branch_name)

        _progress(job_id, "clone", 0.1, "cloning working copy")
        _pr_cmd(["git", "clone", str(root), str(clone_dir)], token=token)
        _pr_cmd(
            ["git", "-C", str(clone_dir), "checkout", "--detach", source_revision.removeprefix("git:")],
            token=token,
        )

        _progress(job_id, "patch", 0.35, "applying diff")
        _pr_cmd(["git", "-C", str(clone_dir), "checkout", "-b", branch], token=token)
        _pr_cmd(["git", "-C", str(clone_dir), "apply", "--check"], input_text=diff_text, token=token)
        _pr_cmd(["git", "-C", str(clone_dir), "apply"], input_text=diff_text, token=token)
        _pr_cmd(["git", "-C", str(clone_dir), "add", "-A"], token=token)
        status = _pr_cmd(["git", "-C", str(clone_dir), "status", "--short"], token=token)
        if not status.stdout.strip():
            raise RuntimeError("Diff produced no changes")

        _progress(job_id, "commit", 0.55, "committing diff")
        _pr_cmd(["git", "-C", str(clone_dir), "config", "user.email", "ripple@example.invalid"], token=token)
        _pr_cmd(["git", "-C", str(clone_dir), "config", "user.name", "RIPPLE"], token=token)
        _pr_cmd(["git", "-C", str(clone_dir), "commit", "-m", title or "RIPPLE fix"], token=token)

        _progress(job_id, "push", 0.75, "pushing branch")
        remote = f"https://x-access-token:{token}@github.com/{pr_repo}.git"
        _pr_cmd(["git", "-C", str(clone_dir), "remote", "set-url", "origin", remote], token=token)
        _pr_cmd(["git", "-C", str(clone_dir), "push", "origin", branch], token=token)

        _progress(job_id, "pr", 0.9, "opening draft PR")
        env = dict(os.environ)
        env.setdefault("GH_TOKEN", token)
        pr = _pr_cmd(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                pr_repo,
                "--head",
                branch,
                "--title",
                title or "RIPPLE fix",
                "--body",
                body or "",
                "--draft",
            ],
            env=env,
            token=token,
        )
        pr_url = pr.stdout.strip()
        _set_job(
            job_id,
            status="done",
            progress={"stage": "done", "pct": 1.0, "detail": "draft PR opened"},
            result={
                "repo_id": repo_id,
                "verify_job_id": verify_job_id,
                "source_revision": source_revision,
                "pr_url": pr_url,
                "branch_name": branch,
                "skipped": False,
            },
            finished_at=time.time(),
        )
    except Exception as exc:
        logger.exception("Open PR job %s failed", job_id)
        _set_job(
            job_id,
            status="failed",
            progress={"stage": "failed", "pct": 1.0, "detail": str(exc)},
            error=str(exc),
            finished_at=time.time(),
        )
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


def _set_job(job_id: str, **updates: Any) -> None:
    with _jobs_lock:
        record = _jobs.setdefault(job_id, {"job_id": job_id, "status": "running"})
        record.update(updates)


def _progress(job_id: str, stage: str, pct: float, detail: str) -> None:
    _set_job(job_id, status="running", progress={"stage": stage, "pct": pct, "detail": detail})


def _prepare_repo(repo: str, repo_id: str) -> Path:
    if _is_git_url(repo):
        work_root = Path(".ripple_work")
        work_root.mkdir(exist_ok=True)
        clone_dir = work_root / f"{_slug(repo_id)}-{uuid.uuid4().hex[:8]}"
        subprocess.run(["git", "clone", "--depth", "1", repo, str(clone_dir)], check=True)
        return clone_dir.resolve()

    repo_path = Path(repo).expanduser().resolve()
    if not repo_path.exists() or not repo_path.is_dir():
        raise FileNotFoundError(f"Repository path does not exist: {repo}")
    return repo_path


def _is_git_url(value: str) -> bool:
    if "://" in value or value.startswith("git@"):
        return True
    return value.endswith(".git") and not Path(value).exists()


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return slug or "repo"


def _repo_root_for(repo_id: str) -> Path:
    rows = read_records(
        _driver,
        """
        MATCH (repo:Repo {repo_id: $repo_id})
        RETURN repo.root_path AS root_path
        LIMIT 1
        """,
        {"repo_id": repo_id},
    )
    if not rows:
        raise ValueError(f"Unknown repo_id: {repo_id}")
    root_path = str(rows[0].get("root_path") or "").strip()
    if not root_path:
        raise ValueError(f"Repo {repo_id} is missing root_path; re-ingest it before fix-mode")
    root = Path(root_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Repo root_path does not exist: {root}")
    return root


def _read_function_source(repo_id: str, fqn: str) -> dict[str, Any]:
    rows = read_records(
        _driver,
        """
        MATCH (repo:Repo {repo_id: $repo_id})
        MATCH (f:Function {repo_id: $repo_id, fqn: $fqn})
        RETURN repo.root_path AS root_path,
               f.file_path AS file_path,
               f.start_line AS start_line,
               f.end_line AS end_line
        LIMIT 1
        """,
        {"repo_id": repo_id, "fqn": fqn},
    )
    if not rows:
        raise ValueError(f"Unknown function fqn for repo {repo_id}: {fqn}")
    row = rows[0]
    root_path = str(row.get("root_path") or "").strip()
    if not root_path:
        raise ValueError(f"Repo {repo_id} is missing root_path; re-ingest it before fix-mode")
    file_path = str(row.get("file_path") or "")
    start_line = int(row.get("start_line") or 1)
    end_line = int(row.get("end_line") or start_line)

    root = Path(root_path).expanduser().resolve()
    source_file = (root / file_path).resolve()
    if source_file != root and root not in source_file.parents:
        raise ValueError(f"Function path escapes repository root: {file_path}")
    if not source_file.exists() or not source_file.is_file():
        raise FileNotFoundError(f"Function source file does not exist: {source_file}")

    lines = source_file.read_text(encoding="utf-8").splitlines()
    safe_start = max(start_line, 1)
    safe_end = min(max(end_line, safe_start), len(lines))
    source = "\n".join(lines[safe_start - 1 : safe_end])
    return {
        "repo_id": repo_id,
        "fqn": fqn,
        "file_path": file_path,
        "start_line": safe_start,
        "end_line": safe_end,
        "source": source,
    }


def _register_sandbox(repo_id: str, sandbox: Sandbox, source_revision: str) -> str:
    if not source_revision:
        raise ValueError("source_revision is required")
    sandbox_id = uuid.uuid4().hex[:8]
    now = time.time()
    with _sandboxes_lock:
        _sandboxes[sandbox_id] = SandboxRecord(
            sandbox_id=sandbox_id,
            repo_id=repo_id,
            sandbox=sandbox,
            created_at=now,
            touched_at=now,
            source_revision=source_revision,
        )
    return sandbox_id


def _get_sandbox(sandbox_id: str, *, repo_id: str | None = None) -> Sandbox:
    return _get_sandbox_record(sandbox_id, repo_id=repo_id).sandbox


def _get_sandbox_record(sandbox_id: str, *, repo_id: str | None = None) -> SandboxRecord:
    with _sandboxes_lock:
        record = _sandboxes.get(sandbox_id)
        if record is None:
            raise ValueError(f"Unknown sandbox_id: {sandbox_id}")
        if repo_id is not None and record.repo_id != repo_id:
            raise ValueError(f"Sandbox {sandbox_id} does not belong to repo_id: {repo_id}")
        record.touched_at = time.time()
        return record


def _reserve_sandbox_operation(sandbox_id: str, *, repo_id: str | None = None) -> SandboxRecord:
    """Keep a sandbox alive from queueing through completion of one operation."""
    with _sandboxes_lock:
        record = _sandboxes.get(sandbox_id)
        if record is None:
            raise ValueError(f"Unknown sandbox_id: {sandbox_id}")
        if repo_id is not None and record.repo_id != repo_id:
            raise ValueError(f"Sandbox {sandbox_id} does not belong to repo_id: {repo_id}")
        record.active_operations += 1
        record.touched_at = time.time()
        return record


def _release_sandbox_operation(record: SandboxRecord) -> None:
    with _sandboxes_lock:
        if _sandboxes.get(record.sandbox_id) is record:
            record.active_operations = max(0, record.active_operations - 1)
            record.touched_at = time.time()


def _release_sandbox_operation_by_id(sandbox_id: str) -> None:
    with _sandboxes_lock:
        record = _sandboxes.get(sandbox_id)
        if record is not None:
            record.active_operations = max(0, record.active_operations - 1)
            record.touched_at = time.time()


def _destroy_idle_sandboxes() -> None:
    now = time.time()
    expired: list[SandboxRecord] = []
    with _sandboxes_lock:
        for sandbox_id, record in list(_sandboxes.items()):
            lock_is_held = getattr(record.operation_lock, "locked", lambda: False)()
            if (
                record.active_operations == 0
                and not lock_is_held
                and now - record.touched_at >= _SANDBOX_IDLE_SECONDS
            ):
                expired.append(_sandboxes.pop(sandbox_id))
    for record in expired:
        try:
            record.sandbox.destroy()
        except Exception:
            logger.warning("Failed to destroy idle sandbox %s", record.sandbox_id, exc_info=True)


def _destroy_all_sandboxes() -> None:
    with _sandboxes_lock:
        records = list(_sandboxes.values())
        _sandboxes.clear()
    for record in records:
        try:
            record.sandbox.destroy()
        except Exception:
            logger.warning("Failed to destroy sandbox %s", record.sandbox_id, exc_info=True)


def _normalize_request_id(request_id: str) -> str:
    normalized = (request_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", normalized):
        raise ValueError("request_id must be 16-128 URL-safe characters")
    return normalized


def _cognee_reference(response: object | None) -> str | None:
    if not isinstance(response, dict):
        return None
    for key in ("dataset_id", "pipeline_run_id", "id"):
        value = response.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _build_fix_payload(
    result: dict[str, Any],
    pr_url: str | None,
    impact: list[dict[str, Any]],
    cypher_used: list[str],
    iterations: int,
    memory_notes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    log_tail = str(result.get("log_tail") or "")
    return {
        "mode": "fix",
        "diff": str(result.get("diff") or result.get("diff_text") or ""),
        "verification": {
            "cmd": str(result.get("cmd") or ""),
            "passed": bool(result.get("passed")),
            "log_tail_last_20_lines": "\n".join(log_tail.splitlines()[-20:]),
        },
        "pr_url": pr_url,
        "impact": impact,
        "cypher_used": cypher_used,
        "iterations": max(1, min(int(iterations), 3)),
        "memory_notes": memory_notes or [],
    }


def _proof_context_for_fqns(
    repo_id: str,
    fqns: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    if not fqns:
        return [], []
    try:
        blast = asdict(core_blast_radius(_driver, repo_id, fqns[0], max_hops=4))
    except Exception:
        logger.warning("Unable to attach blast context to verified proof", exc_info=True)
        return [], []
    impact = blast.get("impacted")
    cypher = blast.get("cypher")
    return (
        impact if isinstance(impact, list) else [],
        [str(cypher)] if isinstance(cypher, str) and cypher else [],
    )


def _store_finalized_fix(
    request_id: str,
    verify_job_id: str,
    payload: dict[str, Any],
) -> None:
    normalized = _normalize_request_id(request_id)
    now = time.time()
    with _finalized_fixes_condition:
        _prune_finalized_fixes(now)
        existing = _finalized_fixes.get(normalized)
        if existing is not None and existing[1] != verify_job_id:
            raise ValueError(f"request_id already finalized by another verification: {normalized}")
        _finalized_fixes[normalized] = (now, verify_job_id, copy.deepcopy(payload))
        while len(_finalized_fixes) > _FINALIZED_FIX_MAX:
            oldest = min(_finalized_fixes, key=lambda key: _finalized_fixes[key][0])
            del _finalized_fixes[oldest]
        _finalized_fixes_condition.notify_all()


def _prune_finalized_fixes(now: float) -> None:
    expired = [
        request_id
        for request_id, (created_at, _verify_job_id, _payload) in _finalized_fixes.items()
        if now - created_at >= _FINALIZED_FIX_TTL_SECONDS
    ]
    for request_id in expired:
        del _finalized_fixes[request_id]


def _reap_orphaned_local_sandboxes() -> None:
    with _sandboxes_lock:
        active = {
            record.sandbox.container_name
            for record in _sandboxes.values()
            if isinstance(record.sandbox, LocalDockerSandbox)
        }
    try:
        reap_orphaned_local_docker_sandboxes(active)
    except Exception:
        logger.warning("Failed to reap stale local Docker sandboxes", exc_info=True)


_previous_signal_handlers: dict[int, Any] = {}


def _handle_shutdown_signal(signum: int, frame: Any) -> None:
    """Release sandboxes before preserving the process's prior signal behavior."""
    _destroy_all_sandboxes()
    previous = _previous_signal_handlers.get(signum, signal.SIG_DFL)
    if previous is signal.SIG_IGN:
        return
    if callable(previous):
        previous(signum, frame)
        return
    raise SystemExit(128 + signum)


def _install_shutdown_handlers() -> None:
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            _previous_signal_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, _handle_shutdown_signal)
        except ValueError:
            # Importing the module from a worker thread cannot install handlers.
            logger.debug("Unable to install sandbox cleanup handler for signal %s", signum)


def _sandbox_cleaner() -> None:
    while True:
        time.sleep(60)
        _destroy_idle_sandboxes()
        _reap_orphaned_local_sandboxes()


def _normalize_pr_branch(branch_name: str) -> str:
    if re.fullmatch(r"ripple/fix-[A-Za-z0-9._-]+", branch_name or ""):
        return branch_name
    return f"ripple/fix-{uuid.uuid4().hex[:8]}"


def _verified_proof_for_pr(repo_id: str, verify_job_id: str, diff_text: str) -> tuple[str, str]:
    """Return canonical diff and immutable base for a successful verification job."""
    if not verify_job_id or not verify_job_id.strip():
        raise ValueError("verify_job_id is required")
    if not diff_text or not diff_text.strip():
        raise ValueError("diff_text is required")

    with _jobs_lock:
        record = dict(_jobs.get(verify_job_id, {}))
    if not record:
        raise ValueError(f"Unknown verify_job_id: {verify_job_id}")
    if record.get("kind") != "verify_patch":
        raise ValueError(f"Job is not a verification job: {verify_job_id}")
    if record.get("status") != "done":
        raise ValueError(f"Verification job is not done: {verify_job_id}")

    result = record.get("result")
    if not isinstance(result, dict):
        raise ValueError(f"Verification job has no result: {verify_job_id}")
    if result.get("repo_id") != repo_id:
        raise ValueError(f"Verification job does not belong to repo_id: {repo_id}")
    if result.get("applied") is not True or result.get("passed") is not True:
        raise ValueError(f"Verification job was not successful: {verify_job_id}")

    verified_diff = result.get("diff")
    if not isinstance(verified_diff, str) or not verified_diff.strip():
        raise ValueError(f"Verification job has no verified diff: {verify_job_id}")
    if diff_text != verified_diff:
        raise ValueError("diff_text must exactly match the verification job's verified diff")
    source_revision = result.get("source_revision")
    if not isinstance(source_revision, str) or not source_revision.strip():
        raise ValueError(f"Verification job has no source revision: {verify_job_id}")
    return verified_diff, source_revision


def _validated_pr_provenance(
    pr_job_id: str,
    repo_id: str,
    verify_job_id: str,
    source_revision: str,
    diff_text: str,
) -> None:
    """Ensure the worker still acts on the proof and base captured when it was queued."""
    with _jobs_lock:
        pr_record = dict(_jobs.get(pr_job_id, {}))
    if not pr_record or pr_record.get("kind") != "open_draft_pr":
        raise ValueError(f"Job is not a draft PR job: {pr_job_id}")
    result = pr_record.get("result")
    if not isinstance(result, dict):
        raise ValueError(f"Draft PR job has no provenance: {pr_job_id}")
    if (
        result.get("repo_id") != repo_id
        or result.get("verify_job_id") != verify_job_id
        or result.get("source_revision") != source_revision
    ):
        raise ValueError(f"Draft PR job provenance does not match: {pr_job_id}")

    verified_diff, verified_revision = _verified_proof_for_pr(repo_id, verify_job_id, diff_text)
    if verified_diff != diff_text or verified_revision != source_revision:
        raise ValueError(f"Draft PR job proof no longer matches: {pr_job_id}")


def _pr_url_for_finalize(verify_job_id: str, pr_job_id: str | None) -> str | None:
    if pr_job_id is None:
        return None
    if not pr_job_id.strip():
        raise ValueError("pr_job_id must be a non-empty job id or null")
    with _jobs_lock:
        record = dict(_jobs.get(pr_job_id, {}))
    if not record:
        raise ValueError(f"Unknown pr_job_id: {pr_job_id}")
    if record.get("kind") != "open_draft_pr":
        raise ValueError(f"Job is not a draft PR job: {pr_job_id}")
    if record.get("status") != "done":
        raise ValueError(f"Draft PR job is not done: {pr_job_id}")
    result = record.get("result")
    if not isinstance(result, dict) or result.get("verify_job_id") != verify_job_id:
        raise ValueError(f"Draft PR job does not belong to verification job: {pr_job_id}")
    pr_url = result.get("pr_url")
    if not isinstance(pr_url, str) or not pr_url.strip():
        raise ValueError(f"Draft PR job has no PR URL: {pr_job_id}")
    return pr_url


def _source_revision_or_content_identity(root: Path) -> str:
    """Identify the exact source content used to construct a sandbox snapshot."""
    revision = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if revision.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", revision.stdout.strip()):
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        if status.returncode == 0 and not status.stdout.strip():
            return f"git:{revision.stdout.strip()}"
    return f"content:{_content_identity(root)}"


def _content_identity(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        digest.update(str(relative).replace("\\", "/").encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _require_exact_git_revision(root: Path, source_revision: str) -> None:
    if not source_revision.startswith("git:"):
        raise ValueError("Verified source base is not a clean Git revision; cannot open a PR")
    if _source_revision_or_content_identity(root) != source_revision:
        raise ValueError("Source revision changed since sandbox verification")


def _require_clean_git_checkout(root: Path) -> None:
    """Reject source roots that cannot produce a faithful clone for a verified patch."""
    is_git = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if is_git.returncode != 0 or is_git.stdout.strip() != "true":
        raise ValueError(f"Source checkout is not a Git work tree: {root}")

    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if status.returncode != 0:
        raise RuntimeError(f"Unable to inspect source checkout status: {root}")
    if status.stdout.strip():
        raise ValueError(f"Source checkout has uncommitted changes: {root}")


def _pr_cmd(
    args: list[str],
    *,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    token: str | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        output = _redact((result.stdout or "") + (result.stderr or ""), token)
        raise RuntimeError(output.strip() or f"Command failed: {args[0]}")
    return result


def _redact(value: str, token: str | None) -> str:
    if token:
        return value.replace(token, "***")
    return value


atexit.register(_destroy_all_sandboxes)
_install_shutdown_handlers()
Thread(target=_sandbox_cleaner, name="ripple-sandbox-cleaner", daemon=True).start()
