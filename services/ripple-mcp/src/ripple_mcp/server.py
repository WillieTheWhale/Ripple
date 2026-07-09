"""MCP tools exposing RIPPLE ingest and query capabilities."""

from __future__ import annotations

import logging
import re
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from threading import Lock
from typing import Any, Literal, TypedDict

from mcp.server.fastmcp import FastMCP

from ripple.db import create_driver
from ripple.ingest.covers import add_static_covers
from ripple.ingest.extract import extract
from ripple.ingest.mapping import map_graph
from ripple.ingest.risk import apply_risk
from ripple.ingest.writer import IngestWriter, WriterConfig
from ripple.query.blast import blast_radius as core_blast_radius
from ripple.query.blast import resolve_fqn
from ripple.query.graphview import repo_stats as core_repo_stats

logger = logging.getLogger(__name__)

JobState = Literal["running", "done", "failed"]


class Progress(TypedDict):
    stage: str
    pct: float
    detail: str


class JobRecord(TypedDict, total=False):
    job_id: str
    status: JobState
    progress: Progress
    result: dict[str, Any]
    error: str
    started_at: float
    finished_at: float


_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ripple-ingest")
_jobs: dict[str, JobRecord] = {}
_jobs_lock = Lock()
_driver = create_driver()

mcp = FastMCP(
    "ripple",
    instructions="RIPPLE graph ingest and read-only analysis tools.",
    host="127.0.0.1",
    port=8790,
    streamable_http_path="/mcp",
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


def main() -> int:
    """Run the MCP server on the Streamable HTTP transport."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logger.info("Starting RIPPLE MCP on http://127.0.0.1:8790/mcp")
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
