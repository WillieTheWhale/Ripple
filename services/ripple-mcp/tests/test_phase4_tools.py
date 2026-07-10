from __future__ import annotations

import signal
import subprocess
from pathlib import Path
from threading import Event, Lock, Thread

import pytest

from ripple.sandbox.verify import VerifyResult
from ripple_mcp import server


class FakeSandbox:
    def __init__(self) -> None:
        self.destroy_calls = 0

    def destroy(self) -> None:
        self.destroy_calls += 1


class ProofSandbox(FakeSandbox):
    def __init__(self, diffs: list[str]) -> None:
        super().__init__()
        self._diffs = iter(diffs)

    def run_command(self, command: str, cwd: str | None = None, timeout: int = 15) -> dict[str, object]:
        assert command == "git diff --no-color"
        assert cwd == "/work/repo"
        assert timeout == 15
        return {"exit_code": 0, "output": next(self._diffs), "truncated": False}


@pytest.fixture(autouse=True)
def reset_server_state() -> None:
    with server._jobs_lock:
        server._jobs.clear()
    with server._finalized_fixes_lock:
        server._finalized_fixes.clear()
    with server._sandboxes_lock:
        server._sandboxes.clear()
    yield
    with server._jobs_lock:
        server._jobs.clear()
    with server._finalized_fixes_lock:
        server._finalized_fixes.clear()
    server._destroy_all_sandboxes()


def _successful_verify_job(repo_id: str, diff: str) -> None:
    server._jobs["verify-job"] = {
        "job_id": "verify-job",
        "kind": "verify_patch",
        "status": "done",
        "result": {
            "repo_id": repo_id,
            "applied": True,
            "passed": True,
            "diff": diff,
            "source_revision": "git:" + "a" * 40,
        },
    }


def test_verify_patch_rejects_sandbox_from_another_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox_id = server._register_sandbox("repo-a", FakeSandbox(), "content:test")
    submitted: list[object] = []
    monkeypatch.setattr(server._executor, "submit", lambda *args: submitted.append(args))

    with pytest.raises(ValueError, match="does not belong to repo_id"):
        server.verify_patch(
            "repo-b",
            sandbox_id,
            "request_1234567890",
            "diff --git a/a b/a",
            [],
        )

    assert submitted == []


def test_verify_job_replays_the_canonical_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    canonical = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
    sandbox_id = server._register_sandbox("repo-a", ProofSandbox([]), "content:test")
    calls: list[str] = []
    verified = VerifyResult(True, True, 0, "pytest", "1 passed", 10, canonical)
    monkeypatch.setattr(server, "core_scoped_tests", lambda *args: ["tests/test_a.py"])
    monkeypatch.setattr(
        server,
        "core_verify_patch",
        lambda _sandbox, diff, _tests: calls.append(diff) or verified,
    )

    server._run_verify_patch_job("verify-job", "repo-a", sandbox_id, "candidate", ["a.py:f"])

    result = server._jobs["verify-job"]["result"]
    assert result["passed"] is True
    assert result["proof_replayed"] is True
    assert result["diff"] == canonical
    assert calls == ["candidate", canonical]


def test_successful_request_bound_verify_publishes_recoverable_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
    sandbox_id = server._register_sandbox("repo-a", ProofSandbox([]), "content:test")
    verified = VerifyResult(True, True, 0, "pytest", "1 passed", 10, canonical)
    monkeypatch.delenv("RIPPLE_PR_REPO", raising=False)
    monkeypatch.setattr(server, "core_scoped_tests", lambda *_args: ["tests/test_a.py"])
    monkeypatch.setattr(server, "core_verify_patch", lambda *_args: verified)
    monkeypatch.setattr(
        server,
        "_proof_context_for_fqns",
        lambda *_args: ([{"fqn": "a.py:f"}], ["MATCH (f) RETURN f"]),
    )

    server._run_verify_patch_job(
        "verify-job",
        "repo-a",
        sandbox_id,
        "candidate",
        ["a.py:f"],
        "request_1234567890",
    )

    recovered = server.get_finalized_fix_result("request_1234567890")
    assert recovered["verification"]["passed"] is True
    assert recovered["diff"] == canonical
    assert recovered["impact"] == [{"fqn": "a.py:f"}]


def test_composite_verify_owns_snapshot_sandbox_and_recoverable_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
    sandbox = FakeSandbox()

    class Provider:
        def ensure_snapshot(self, repo_id: str, repo_path: str) -> str:
            assert repo_id == "repo-a"
            assert repo_path == "/repo"
            return "snapshot"

        def create(self, repo_id: str) -> FakeSandbox:
            assert repo_id == "repo-a"
            return sandbox

    verified = VerifyResult(True, True, 0, "pytest", "1 passed", 10, canonical)
    monkeypatch.setattr(server, "_repo_root_for", lambda _repo_id: Path("/repo"))
    monkeypatch.setattr(server, "_source_revision_or_content_identity", lambda _root: "content:x")
    monkeypatch.setattr(server, "provider_from_env", Provider)
    monkeypatch.setattr(server, "core_scoped_tests", lambda *_args: ["tests/test_a.py"])
    monkeypatch.setattr(server, "core_verify_patch", lambda *_args: verified)
    monkeypatch.setattr(server, "_proof_context_for_fqns", lambda *_args: ([], ["MATCH"]))

    server._run_verify_fix_job(
        "verify-job",
        "repo-a",
        "request_1234567890",
        "candidate",
        ["a.py:f"],
    )

    assert server._jobs["verify-job"]["status"] == "done"
    assert server.get_finalized_fix_result("request_1234567890")["diff"] == canonical
    assert sandbox.destroy_calls == 1


def test_verify_job_rejects_canonical_diff_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    first = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
    drifted = "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n"
    sandbox_id = server._register_sandbox("repo-a", ProofSandbox([]), "content:test")
    verified = iter(
        [
            VerifyResult(True, True, 0, "pytest", "1 passed", 10, first),
            VerifyResult(True, True, 0, "pytest", "1 passed", 10, drifted),
        ]
    )
    monkeypatch.setattr(server, "core_scoped_tests", lambda *args: ["tests/test_a.py"])
    monkeypatch.setattr(server, "core_verify_patch", lambda *_args: next(verified))

    server._run_verify_patch_job("verify-job", "repo-a", sandbox_id, "candidate", ["a.py:f"])

    result = server._jobs["verify-job"]["result"]
    assert result["passed"] is False
    assert result["proof_replayed"] is True
    assert result["log_tail"] == "Canonical diff changed during proof replay"


def test_verify_jobs_serialize_verification_for_the_same_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ObservedLock:
        def __init__(self) -> None:
            self._lock = Lock()
            self._attempts = 0

        def __enter__(self) -> "ObservedLock":
            self._attempts += 1
            if self._attempts == 2:
                second_lock_attempt.set()
            self._lock.acquire()
            return self

        def __exit__(self, *_args: object) -> None:
            self._lock.release()

    sandbox_id = server._register_sandbox("repo-a", FakeSandbox(), "content:test")
    record = server._get_sandbox_record(sandbox_id, repo_id="repo-a")
    first_verify_entered = Event()
    second_lock_attempt = Event()
    allow_first_verify_to_finish = Event()
    verifier_state_lock = Lock()
    active_verifications = 0
    max_active_verifications = 0
    verify_calls = 0
    record.operation_lock = ObservedLock()

    def verify_patch(*_args: object) -> VerifyResult:
        nonlocal active_verifications, max_active_verifications, verify_calls
        with verifier_state_lock:
            verify_calls += 1
            call_number = verify_calls
            active_verifications += 1
            max_active_verifications = max(max_active_verifications, active_verifications)

        try:
            if call_number == 1:
                first_verify_entered.set()
                assert allow_first_verify_to_finish.wait(timeout=5)
            return VerifyResult(False, False, 1, "pytest", "failed", 1)
        finally:
            with verifier_state_lock:
                active_verifications -= 1

    monkeypatch.setattr(server, "core_scoped_tests", lambda *_args: [])
    monkeypatch.setattr(server, "core_verify_patch", verify_patch)

    first = Thread(
        target=server._run_verify_patch_job,
        args=("verify-job-1", "repo-a", sandbox_id, "first", []),
    )
    second = Thread(
        target=server._run_verify_patch_job,
        args=("verify-job-2", "repo-a", sandbox_id, "second", []),
    )

    try:
        first.start()
        assert first_verify_entered.wait(timeout=5)
        second.start()
        assert second_lock_attempt.wait(timeout=5)
        with verifier_state_lock:
            assert verify_calls == 1
            assert active_verifications == 1
            assert max_active_verifications == 1
    finally:
        allow_first_verify_to_finish.set()
        first.join(timeout=5)
        second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert verify_calls == 2
    assert max_active_verifications == 1


def test_open_draft_pr_requires_matching_successful_verified_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified_diff = "diff --git a/a.py b/a.py\n"
    _successful_verify_job("repo-a", verified_diff)
    monkeypatch.delenv("RIPPLE_PR_REPO", raising=False)

    with pytest.raises(ValueError, match="exactly match"):
        server.open_draft_pr(
            "repo-a", "verify-job", "ripple/fix-12345678", "unverified diff", "title", "body"
        )

    server._jobs["verify-job"]["result"] = {
        "repo_id": "repo-a",
        "applied": True,
        "passed": False,
        "diff": verified_diff,
        "source_revision": "git:" + "a" * 40,
    }
    with pytest.raises(ValueError, match="not successful"):
        server.open_draft_pr(
            "repo-a", "verify-job", "ripple/fix-12345678", verified_diff, "title", "body"
        )


def test_open_draft_pr_submits_only_the_verified_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    verified_diff = "diff --git a/a.py b/a.py\n"
    _successful_verify_job("repo-a", verified_diff)
    monkeypatch.setenv("RIPPLE_PR_REPO", "owner/repo")
    submitted: list[tuple[object, ...]] = []
    monkeypatch.setattr(server._executor, "submit", lambda *args: submitted.append(args))

    response = server.open_draft_pr(
        "repo-a", "verify-job", "ripple/fix-12345678", verified_diff, "title", "body"
    )

    assert response["status"] == "running"
    assert len(submitted) == 1
    assert submitted[0][0] is server._run_open_pr_job
    assert submitted[0][4] == "git:" + "a" * 40
    assert submitted[0][6] == verified_diff


def test_finalize_fix_result_requires_a_verification_job() -> None:
    server._jobs["other-job"] = {
        "job_id": "other-job",
        "kind": "open_draft_pr",
        "status": "done",
        "result": {},
    }

    with pytest.raises(ValueError, match="not a verification job"):
        server.finalize_fix_result("other-job", None, [], [], 1)


def test_finalize_fix_result_derives_pr_url_from_matching_job() -> None:
    _successful_verify_job("repo-a", "diff\n")
    server._jobs["pr-job"] = {
        "job_id": "pr-job",
        "kind": "open_draft_pr",
        "status": "done",
        "result": {"verify_job_id": "other-verify", "pr_url": "https://example.test/pr/1"},
    }

    with pytest.raises(ValueError, match="does not belong"):
        server.finalize_fix_result("verify-job", "pr-job", [], [], 1)

    server._jobs["pr-job"]["result"]["verify_job_id"] = "verify-job"
    result = server.finalize_fix_result("verify-job", "pr-job", [], [], 1)

    assert result["pr_url"] == "https://example.test/pr/1"


def test_finalize_fix_result_is_recoverable_only_by_its_request_id() -> None:
    _successful_verify_job("repo-a", "diff\n")

    finalized = server.finalize_fix_result(
        "verify-job",
        None,
        [],
        [],
        1,
        "request_1234567890",
    )

    assert server.get_finalized_fix_result("request_1234567890") == finalized
    with pytest.raises(ValueError, match="No finalized fix result"):
        server.get_finalized_fix_result("request_0987654321")


def test_request_id_cannot_be_rebound_to_another_verification() -> None:
    _successful_verify_job("repo-a", "diff\n")
    server.finalize_fix_result("verify-job", None, [], [], 1, "request_1234567890")
    server._jobs["verify-job-2"] = {
        **server._jobs["verify-job"],
        "job_id": "verify-job-2",
    }

    with pytest.raises(ValueError, match="already finalized"):
        server.finalize_fix_result(
            "verify-job-2",
            None,
            [],
            [],
            1,
            "request_1234567890",
        )


def test_idle_cleanup_preserves_reserved_sandbox() -> None:
    sandbox = FakeSandbox()
    sandbox_id = server._register_sandbox("repo-a", sandbox, "content:test")
    record = server._reserve_sandbox_operation(sandbox_id)
    record.touched_at = 0

    server._destroy_idle_sandboxes()

    assert server._get_sandbox_record(sandbox_id) is record
    assert sandbox.destroy_calls == 0

    server._release_sandbox_operation(record)
    record.touched_at = 0
    server._destroy_idle_sandboxes()

    with pytest.raises(ValueError, match="Unknown sandbox_id"):
        server._get_sandbox_record(sandbox_id)
    assert sandbox.destroy_calls == 1


def test_orphan_reaper_receives_current_local_container_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = server.LocalDockerSandbox("ripple-sbx-run-live", "image", "repo-a")
    monkeypatch.setattr(local, "destroy", lambda: None)
    server._register_sandbox("repo-a", local, "content:test")
    observed: list[set[str]] = []
    monkeypatch.setattr(
        server,
        "reap_orphaned_local_docker_sandboxes",
        lambda active: observed.append(set(active)),
    )

    server._reap_orphaned_local_sandboxes()

    assert observed == [{"ripple-sbx-run-live"}]


def test_open_pr_rejects_dirty_source_before_cloning(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[object] = []
    monkeypatch.setenv("GH_TOKEN", "token")
    monkeypatch.setattr(server, "_repo_root_for", lambda _repo_id: Path("/source"))
    monkeypatch.setattr(
        server,
        "_require_clean_git_checkout",
        lambda _root: (_ for _ in ()).throw(ValueError("Source checkout has uncommitted changes")),
    )
    monkeypatch.setattr(server, "_validated_pr_provenance", lambda *_args: None)
    monkeypatch.setattr(server, "_pr_cmd", lambda *args, **kwargs: commands.append((args, kwargs)))

    server._run_open_pr_job(
        "pr-job",
        "repo-a",
        "verify-job",
        "git:" + "a" * 40,
        "ripple/fix-12345678",
        "diff",
        "title",
        "body",
        "owner/repo",
    )

    assert commands == []
    assert server._jobs["pr-job"]["status"] == "failed"
    assert "uncommitted changes" in server._jobs["pr-job"]["error"]


def test_clean_git_checkout_rejects_non_git_and_dirty_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    non_git = subprocess.CompletedProcess([], 128, "", "not a git repository")
    monkeypatch.setattr(server.subprocess, "run", lambda *args, **kwargs: non_git)
    with pytest.raises(ValueError, match="not a Git work tree"):
        server._require_clean_git_checkout(Path("/source"))

    results = iter(
        [
            subprocess.CompletedProcess([], 0, "true\n", ""),
            subprocess.CompletedProcess([], 0, " M src/app.py\n", ""),
        ]
    )
    monkeypatch.setattr(server.subprocess, "run", lambda *args, **kwargs: next(results))
    with pytest.raises(ValueError, match="uncommitted changes"):
        server._require_clean_git_checkout(Path("/source"))


def test_signal_cleanup_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = FakeSandbox()
    server._register_sandbox("repo-a", sandbox, "content:test")
    monkeypatch.setitem(server._previous_signal_handlers, signal.SIGTERM, signal.SIG_IGN)

    server._handle_shutdown_signal(signal.SIGTERM, None)
    server._destroy_all_sandboxes()  # The atexit path must not destroy the same sandbox again.

    assert sandbox.destroy_calls == 1
