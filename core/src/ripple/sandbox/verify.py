"""Patch verification helpers for sandboxed RIPPLE fix-mode."""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from typing import Any

from ripple.db import read_records
from ripple.sandbox.base import Sandbox


@dataclass(frozen=True)
class VerifyResult:
    """Result of applying and testing a patch in a sandbox."""

    applied: bool
    passed: bool
    exit_code: int
    cmd: str
    log_tail: str
    duration_ms: int
    diff: str = ""


def verify_patch(
    sandbox: Sandbox,
    diff_text: str,
    test_paths: list[str],
    max_output: int = 8000,
) -> VerifyResult:
    """Apply *diff_text* in *sandbox* and run scoped pytest paths."""
    started = time.monotonic()
    sandbox.reset()
    normalized_diff = diff_text if diff_text.endswith("\n") else f"{diff_text}\n"
    sandbox.upload_file("/work/patch.diff", normalized_diff)

    check_cmd = "git apply --check /work/patch.diff"
    check = sandbox.run_command(check_cmd, cwd="/work/repo", timeout=60)
    if check["exit_code"] != 0:
        return _result(False, False, check["exit_code"], check_cmd, check["output"], started, max_output)

    apply_cmd = "git apply /work/patch.diff"
    applied = sandbox.run_command(apply_cmd, cwd="/work/repo", timeout=60)
    if applied["exit_code"] != 0:
        return _result(
            False,
            False,
            applied["exit_code"],
            apply_cmd,
            applied["output"],
            started,
            max_output,
        )

    canonical_diff, capture_failure = _canonical_working_tree_diff(sandbox)
    if capture_failure is not None:
        return _result(
            True,
            False,
            capture_failure["exit_code"],
            capture_failure["cmd"],
            capture_failure["output"],
            started,
            max_output,
        )

    cmd = _pytest_cmd(test_paths, include_timeout=True)
    tested = sandbox.run_command(cmd, cwd="/work/repo", timeout=300)
    if _timeout_plugin_unsupported(tested["output"]):
        cmd = _pytest_cmd(test_paths, include_timeout=False)
        tested = sandbox.run_command(cmd, cwd="/work/repo", timeout=300)

    post_test_diff, capture_failure = _canonical_working_tree_diff(sandbox)
    if capture_failure is not None:
        return _result(
            True,
            False,
            capture_failure["exit_code"],
            capture_failure["cmd"],
            capture_failure["output"],
            started,
            max_output,
            canonical_diff,
        )
    if post_test_diff != canonical_diff:
        return _result(
            True,
            False,
            1,
            _CANONICAL_DIFF_CMD,
            "Candidate tree drift detected after tests.",
            started,
            max_output,
            canonical_diff,
        )

    return _result(
        True,
        tested["exit_code"] == 0,
        tested["exit_code"],
        cmd,
        tested["output"],
        started,
        max_output,
        canonical_diff,
    )


def scoped_tests(driver: Any, repo_id: str, fqns: list[str]) -> list[str]:
    """Return distinct test file paths covering *fqns*, capped at 20 files."""
    normalized_fqns = sorted({fqn for fqn in fqns if fqn})
    rows: list[dict[str, Any]] = []
    if normalized_fqns:
        rows = read_records(
            driver,
            """
            MATCH (target:Function {repo_id: $repo_id})
            WHERE target.fqn IN $fqns
            MATCH (test:Test {repo_id: $repo_id})-[:COVERS]->(target)
            RETURN DISTINCT test.file_path AS file_path
            ORDER BY file_path ASC
            LIMIT 20
            """,
            {"repo_id": repo_id, "fqns": normalized_fqns},
        )
    paths = _paths(rows)
    if paths:
        return paths

    fallback_rows = read_records(
        driver,
        """
        MATCH (file:File {repo_id: $repo_id})
        WHERE file.path STARTS WITH 'tests/'
           OR file.path CONTAINS '/tests/'
           OR file.path STARTS WITH 'test_'
           OR file.path ENDS WITH '_test.py'
        RETURN DISTINCT file.path AS file_path
        ORDER BY file_path ASC
        LIMIT 20
        """,
        {"repo_id": repo_id},
    )
    fallback_paths = _paths(fallback_rows)
    return fallback_paths or ["tests"]


def _pytest_cmd(test_paths: list[str], *, include_timeout: bool) -> str:
    paths = [path for path in test_paths if path]
    if not paths:
        paths = ["tests"]
    quoted_paths = " ".join(shlex.quote(path) for path in paths)
    timeout_arg = " --timeout=60" if include_timeout else ""
    return f"python -m pytest {quoted_paths} -q --maxfail=5 -p no:cacheprovider{timeout_arg}"


def _timeout_plugin_unsupported(output: str) -> bool:
    lowered = output.lower()
    return "--timeout" in output and (
        "unrecognized arguments" in lowered or "error: usage" in lowered
    )


_INTENT_TO_ADD_CMD = "git add --intent-to-add -- ."
_CANONICAL_DIFF_CMD = "git diff --binary --no-ext-diff --no-renames HEAD"


def _canonical_working_tree_diff(
    sandbox: Sandbox,
) -> tuple[str, dict[str, int | str] | None]:
    """Return a binary-capable diff of all tracked and untracked tree changes."""
    intent_to_add = sandbox.run_command(_INTENT_TO_ADD_CMD, cwd="/work/repo", timeout=60)
    if intent_to_add["exit_code"] != 0:
        return "", {
            "exit_code": intent_to_add["exit_code"],
            "cmd": _INTENT_TO_ADD_CMD,
            "output": intent_to_add["output"],
        }

    diff = sandbox.run_command(_CANONICAL_DIFF_CMD, cwd="/work/repo", timeout=60)
    if diff["exit_code"] != 0:
        return "", {
            "exit_code": diff["exit_code"],
            "cmd": _CANONICAL_DIFF_CMD,
            "output": diff["output"],
        }
    return diff["output"], None


def _paths(rows: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for row in rows:
        path = str(row.get("file_path") or "").strip()
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths[:20]


def _result(
    applied: bool,
    passed: bool,
    exit_code: int,
    cmd: str,
    output: str,
    started: float,
    max_output: int,
    diff: str = "",
) -> VerifyResult:
    return VerifyResult(
        applied=applied,
        passed=passed,
        exit_code=exit_code,
        cmd=cmd,
        log_tail=output[-max_output:],
        duration_ms=int((time.monotonic() - started) * 1000),
        diff=diff,
    )
