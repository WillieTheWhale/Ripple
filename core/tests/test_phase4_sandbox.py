"""Unit coverage for phase-4 sandbox verification and provider selection."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ripple.sandbox import LocalDockerProvider, provider_from_env
from ripple.sandbox.daytona_provider import DaytonaProvider, _SNAPSHOT_IDS
from ripple.sandbox.local_docker import LocalDockerSandbox, _quote, _run_bounded, _start_container
from ripple.sandbox.verify import _pytest_cmd, verify_patch


_CANONICAL_DIFF_CMD = "git diff --binary --no-ext-diff --no-renames HEAD"


class RecordingSandbox:
    """A deterministic sandbox double that never invokes Docker."""

    def __init__(self, results: dict[str, dict[str, object] | list[dict[str, object]]]) -> None:
        self.results = results
        self.commands: list[str] = []
        self.uploads: list[tuple[str, str]] = []
        self.reset_calls = 0

    def upload_file(self, path: str, content: str) -> None:
        self.uploads.append((path, content))

    def download_file(self, path: str) -> str:
        raise AssertionError(f"Unexpected download: {path}")

    def reset(self) -> None:
        self.reset_calls += 1

    def run_command(
        self,
        cmd: str,
        cwd: str | None = None,
        timeout: int = 300,
    ) -> dict[str, object]:
        del cwd, timeout
        self.commands.append(cmd)
        for prefix, result in self.results.items():
            if cmd.startswith(prefix):
                if isinstance(result, list):
                    if not result:
                        raise AssertionError(f"No remaining result for command: {cmd}")
                    result = result.pop(0)
                return {"truncated": False, **result}
        raise AssertionError(f"Unexpected command: {cmd}")

    def destroy(self) -> None:
        pass


def _successful_results(
    *,
    canonical_diff: str = "diff --git a/a.py b/a.py\n",
    post_test_diff: str | None = None,
    test_exit_code: int = 0,
    test_output: str = "1 passed\n",
) -> dict[str, dict[str, object] | list[dict[str, object]]]:
    return {
        "git apply --check": {"exit_code": 0, "output": ""},
        "git apply /work/patch.diff": {"exit_code": 0, "output": ""},
        "git add --intent-to-add -- .": {"exit_code": 0, "output": ""},
        _CANONICAL_DIFF_CMD: [
            {"exit_code": 0, "output": canonical_diff},
            {"exit_code": 0, "output": post_test_diff if post_test_diff is not None else canonical_diff},
        ],
        "python -m pytest": {"exit_code": test_exit_code, "output": test_output},
    }


def test_verify_patch_applies_and_passes_scoped_tests() -> None:
    sandbox = RecordingSandbox(_successful_results())

    result = verify_patch(sandbox, "diff --git a/a.py b/a.py", ["tests/test app.py"])

    assert result.applied is True
    assert result.passed is True
    assert result.exit_code == 0
    assert result.log_tail == "1 passed\n"
    assert result.diff == "diff --git a/a.py b/a.py\n"
    assert sandbox.reset_calls == 1
    assert sandbox.uploads == [("/work/patch.diff", "diff --git a/a.py b/a.py\n")]
    assert shlex.split(result.cmd) == [
        "python",
        "-m",
        "pytest",
        "tests/test app.py",
        "-q",
        "--maxfail=5",
        "-p",
        "no:cacheprovider",
        "--timeout=60",
    ]


def test_verify_patch_reports_apply_failure_without_running_tests() -> None:
    results = _successful_results()
    results["git apply /work/patch.diff"] = {"exit_code": 1, "output": "patch does not apply"}
    sandbox = RecordingSandbox(results)

    result = verify_patch(sandbox, "bad diff", ["tests"])

    assert result.applied is False
    assert result.passed is False
    assert result.exit_code == 1
    assert result.cmd == "git apply /work/patch.diff"
    assert result.log_tail == "patch does not apply"
    assert all(not command.startswith("python -m pytest") for command in sandbox.commands)


def test_verify_patch_reports_test_failure_after_apply() -> None:
    sandbox = RecordingSandbox(_successful_results(test_exit_code=1, test_output="1 failed\n"))

    result = verify_patch(sandbox, "diff", ["tests"])

    assert result.applied is True
    assert result.passed is False
    assert result.exit_code == 1
    assert result.log_tail == "1 failed\n"


def test_verify_patch_captures_new_file_candidate_in_canonical_diff() -> None:
    new_file_diff = """diff --git a/new.bin b/new.bin
new file mode 100644
index 0000000..3e75765
GIT binary patch
literal 4
LcmZQzWMT#Y01f~L
"""
    sandbox = RecordingSandbox(_successful_results(canonical_diff=new_file_diff))

    result = verify_patch(sandbox, "diff", ["tests"])

    assert result.passed is True
    assert result.diff == new_file_diff
    assert sandbox.commands.count("git add --intent-to-add -- .") == 2
    assert sandbox.commands.count(_CANONICAL_DIFF_CMD) == 2


@pytest.mark.parametrize(
    "post_test_diff",
    [
        "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-old\n+changed-by-test\n",
        "diff --git a/test-output.txt b/test-output.txt\nnew file mode 100644\n",
    ],
    ids=["staged-tracked", "untracked"],
)
def test_verify_patch_rejects_test_induced_candidate_tree_drift(post_test_diff: str) -> None:
    sandbox = RecordingSandbox(
        _successful_results(
            canonical_diff="diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-old\n+candidate\n",
            post_test_diff=post_test_diff,
        )
    )

    result = verify_patch(sandbox, "diff", ["tests"])

    assert result.applied is True
    assert result.passed is False
    assert result.exit_code == 1
    assert result.cmd == _CANONICAL_DIFF_CMD
    assert result.log_tail == "Candidate tree drift detected after tests."
    assert result.diff == "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-old\n+candidate\n"


def test_local_docker_timeout_decodes_bytes_and_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    def time_out(*args: object, **kwargs: object) -> None:
        del args
        output = kwargs["stdout"]
        output.write(b"begin-" + b"x" * 20 + b"-end")
        raise subprocess.TimeoutExpired("docker exec", 1)

    monkeypatch.setattr("ripple.sandbox.local_docker.subprocess.run", time_out)

    result = _run_bounded(["docker", "exec", "unused"], timeout=1, output_limit=12)

    assert result == {"exit_code": 124, "output": "xxxxxxxx-end", "truncated": True}
    assert isinstance(result["output"], str)


def test_start_container_applies_isolation_and_resource_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "ripple.sandbox.local_docker._run",
        lambda args, **kwargs: calls.append(args),
    )

    _start_container("ripple-sbx-repo", "ripple-sbx-run-test", "repo")

    args = calls[0]
    assert ["--network", "none"] == args[args.index("--network") : args.index("--network") + 2]
    assert "--cpus" in args and "--memory" in args and "--pids-limit" in args
    assert ["--cap-drop", "ALL"] == args[args.index("--cap-drop") : args.index("--cap-drop") + 2]
    assert "no-new-privileges" in args


def test_local_reset_recreates_from_image(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "ripple.sandbox.local_docker._run",
        lambda args, **kwargs: calls.append(args),
    )
    sandbox = LocalDockerSandbox("ripple-sbx-run-test", "ripple-sbx-repo", "repo")

    sandbox.reset()

    assert calls[0] == ["docker", "rm", "-f", "ripple-sbx-run-test"]
    assert calls[1][:4] == ["docker", "run", "-d", "--name"]
    assert "ripple-sbx-repo" in calls[1]


def test_provider_from_env_uses_docker_without_daytona_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)

    assert isinstance(provider_from_env(), LocalDockerProvider)


def test_provider_from_env_uses_daytona_when_key_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDaytonaProvider:
        pass

    monkeypatch.setenv("DAYTONA_API_KEY", "test-key")
    monkeypatch.setattr("ripple.sandbox.daytona_provider.DaytonaProvider", FakeDaytonaProvider)

    assert isinstance(provider_from_env(), FakeDaytonaProvider)


def test_daytona_missing_sdk_explains_install_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_sdk(name: str) -> object:
        raise ModuleNotFoundError("No module named 'daytona'", name=name)

    monkeypatch.setenv("DAYTONA_API_KEY", "test-key")
    monkeypatch.setattr("ripple.sandbox.daytona_provider.importlib.import_module", missing_sdk)

    with pytest.raises(RuntimeError, match="ripple\\[daytona\\].*unset DAYTONA_API_KEY"):
        DaytonaProvider()


def test_daytona_adapter_matches_current_sdk_surface(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeParams:
        def __init__(self, **values: object) -> None:
            self.values = values

    class FakeImage:
        @staticmethod
        def from_dockerfile(path: Path) -> Path:
            assert path.name == "Dockerfile"
            return path

    class FakeFileSystem:
        def __init__(self) -> None:
            self.uploaded: tuple[bytes, str] | None = None

        def upload_file(self, src: bytes, dst: str) -> None:
            self.uploaded = (src, dst)

        def download_file(self, path: str) -> bytes:
            return path.encode()

    class FakeSandbox:
        def __init__(self) -> None:
            self.fs = FakeFileSystem()
            self.process = SimpleNamespace(
                exec=lambda *args, **kwargs: SimpleNamespace(result="ok", exit_code=0)
            )

    class FakeNotFoundError(Exception):
        status_code = 404

    class FakeSnapshotService:
        def __init__(self) -> None:
            self.created: list[FakeParams] = []
            self.snapshots: dict[str, SimpleNamespace] = {}

        def get(self, name: str) -> SimpleNamespace:
            try:
                return self.snapshots[name]
            except KeyError as exc:
                raise FakeNotFoundError from exc

        def create(self, params: FakeParams, timeout: int) -> SimpleNamespace:
            assert timeout == 900
            snapshot = SimpleNamespace(name=params.values["name"])
            self.created.append(params)
            self.snapshots[snapshot.name] = snapshot
            return snapshot

    class FakeDaytona:
        instance: "FakeDaytona | None" = None

        def __init__(self, config: object) -> None:
            self.config = config
            self.deleted: list[FakeSandbox] = []
            self.created: list[FakeParams] = []
            self.snapshot = FakeSnapshotService()
            FakeDaytona.instance = self

        def create(self, params: FakeParams, timeout: int) -> FakeSandbox:
            assert timeout == 120
            self.created.append(params)
            return FakeSandbox()

        def delete(self, sandbox: FakeSandbox) -> None:
            self.deleted.append(sandbox)

    module = SimpleNamespace(
        Daytona=FakeDaytona,
        DaytonaConfig=lambda **values: values,
        Image=FakeImage,
        CreateSnapshotParams=FakeParams,
        CreateSandboxFromSnapshotParams=FakeParams,
    )
    monkeypatch.setenv("DAYTONA_API_KEY", "test-key")
    monkeypatch.setattr("ripple.sandbox.daytona_provider.importlib.import_module", lambda _: module)
    _SNAPSHOT_IDS.clear()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "example.py").write_text("value = 1\n")

    provider = DaytonaProvider()
    snapshot_name = provider.ensure_snapshot("repo", str(repo))
    assert snapshot_name.startswith("ripple-sbx-repo-")
    assert provider.ensure_snapshot("repo", str(repo)) == snapshot_name
    assert len(FakeDaytona.instance.snapshot.created) == 1
    sandbox = provider.create("repo")
    assert FakeDaytona.instance.created[-1].values["snapshot"] == snapshot_name
    assert FakeDaytona.instance.created[-1].values["network_block_all"] is True
    sandbox.upload_file("/work/value.txt", "hello")
    assert sandbox._sandbox.fs.uploaded == (b"hello", "/work/value.txt")
    assert sandbox.run_command("true")["output"] == "ok"
    first = sandbox._sandbox
    sandbox.reset()
    assert first in FakeDaytona.instance.deleted
    assert FakeDaytona.instance.created[-1].values["network_block_all"] is True


def test_command_and_path_quoting_round_trips_special_characters() -> None:
    path = "tests/it's a file.py"
    cmd = _pytest_cmd([path], include_timeout=True)

    assert shlex.split(_quote(path)) == [path]
    assert shlex.split(cmd)[3] == path
