"""Optional Daytona SDK sandbox provider.

Install ``ripple[daytona]`` and set ``DAYTONA_API_KEY`` to select this provider;
unset the key to use local Docker instead. The public methods mirror the local
Docker provider so switching providers is a configuration change.
"""

from __future__ import annotations

import importlib
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import weakref
from pathlib import Path
from typing import Any

from ripple.sandbox.base import RunResult
from ripple.sandbox.local_docker import _dockerfile, _materialize_repo, _source_hash


_SNAPSHOT_IDS: dict[str, tuple[str, str]] = {}
_SNAPSHOT_LOCKS: weakref.WeakValueDictionary[tuple[str, str], threading.Lock] = (
    weakref.WeakValueDictionary()
)
_SNAPSHOT_LOCKS_GUARD = threading.Lock()
_OUTPUT_LIMIT = 20_000
_CONFLICT_LOOKUP_ATTEMPTS = 6
_CONFLICT_LOOKUP_DELAY_SECONDS = 0.25


def _snapshot_name(repo_id: str, source_hash: str) -> str:
    slug = re.sub(r"[^a-z0-9_.-]+", "-", repo_id.lower()).strip("-.")[:40]
    return f"ripple-sbx-{slug or 'repo'}-{source_hash[:12]}"


def _run_params(snapshot_params: Any, snapshot_name: str) -> Any:
    return snapshot_params(
        name=f"ripple-sbx-run-{uuid.uuid4().hex[:8]}",
        snapshot=snapshot_name,
        auto_delete_interval=15,
        network_block_all=True,
    )


def _snapshot_lock(repo_id: str, source_hash: str) -> threading.Lock:
    """Return the process-wide lock for a deterministic snapshot identity."""
    key = (repo_id, source_hash)
    with _SNAPSHOT_LOCKS_GUARD:
        lock = _SNAPSHOT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _SNAPSHOT_LOCKS[key] = lock
        return lock


def _snapshot_after_conflict(client: Any, snapshot_name: str) -> Any:
    """Wait for a concurrently-created snapshot to become visible."""
    last_error: Exception | None = None
    for attempt in range(_CONFLICT_LOOKUP_ATTEMPTS):
        try:
            return client.snapshot.get(snapshot_name)
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404:
                raise
            last_error = exc
        if attempt + 1 < _CONFLICT_LOOKUP_ATTEMPTS:
            time.sleep(_CONFLICT_LOOKUP_DELAY_SECONDS * (attempt + 1))
    raise RuntimeError(
        f"Conflicting Daytona snapshot did not become visible: {snapshot_name}"
    ) from last_error


class DaytonaSandbox:
    """Thin adapter around a Daytona sandbox object."""

    def __init__(self, sandbox: Any, client: Any, snapshot_name: str, snapshot_params: Any) -> None:
        self._sandbox = sandbox
        self._client = client
        self._snapshot_name = snapshot_name
        self._snapshot_params = snapshot_params

    def upload_file(self, path: str, content: str) -> None:
        self._sandbox.fs.upload_file(content.encode("utf-8"), path)

    def download_file(self, path: str) -> str:
        data = self._sandbox.fs.download_file(path)
        if isinstance(data, bytes):
            return data.decode("utf-8")
        return str(data)

    def reset(self) -> None:
        """Recreate the sandbox from its immutable Daytona snapshot."""
        self._client.delete(self._sandbox)
        params = _run_params(self._snapshot_params, self._snapshot_name)
        self._sandbox = self._client.create(params, timeout=120)

    def run_command(
        self,
        cmd: str,
        cwd: str | None = None,
        timeout: int = 300,
    ) -> RunResult:
        started = time.monotonic()
        result = self._sandbox.process.exec(cmd, cwd=cwd, timeout=timeout)
        output = str(getattr(result, "result", getattr(result, "output", result)))
        exit_code = int(getattr(result, "exit_code", getattr(result, "code", 0)))
        if time.monotonic() - started >= timeout and exit_code == 0:
            exit_code = 124
        encoded = output.encode("utf-8")
        truncated = len(encoded) > _OUTPUT_LIMIT
        if truncated:
            output = encoded[-_OUTPUT_LIMIT:].decode("utf-8", errors="replace")
        return {"exit_code": exit_code, "output": output, "truncated": truncated}

    def destroy(self) -> None:
        self._client.delete(self._sandbox)


class DaytonaProvider:
    """Daytona-backed implementation activated by ``DAYTONA_API_KEY``."""

    def __init__(self) -> None:
        if not os.environ.get("DAYTONA_API_KEY"):
            raise RuntimeError("DAYTONA_API_KEY is required for DaytonaProvider")
        try:
            module = importlib.import_module("daytona")
        except ModuleNotFoundError as exc:
            if exc.name != "daytona":
                raise
            raise RuntimeError(
                "DAYTONA_API_KEY selected the Daytona sandbox provider, but its optional "
                "SDK is not installed. Install it with `pip install 'ripple[daytona]'`, "
                "or unset DAYTONA_API_KEY to use LocalDockerProvider."
            ) from exc
        required = (
            "Daytona",
            "DaytonaConfig",
            "Image",
            "CreateSnapshotParams",
            "CreateSandboxFromSnapshotParams",
        )
        missing = [name for name in required if not hasattr(module, name)]
        if missing:
            raise RuntimeError(f"Unsupported daytona SDK; missing: {', '.join(missing)}")
        config = module.DaytonaConfig(api_key=os.environ["DAYTONA_API_KEY"])
        self._client = module.Daytona(config)
        self._image_cls = module.Image
        self._create_snapshot_params = module.CreateSnapshotParams
        self._snapshot_params = module.CreateSandboxFromSnapshotParams

    def create(self, repo_id: str) -> DaytonaSandbox:
        cached = _SNAPSHOT_IDS.get(repo_id)
        if cached is None:
            raise RuntimeError(f"No Daytona snapshot for {repo_id}; call ensure_snapshot first")
        snapshot_name = cached[1]
        params = _run_params(self._snapshot_params, snapshot_name)
        sandbox = self._client.create(params, timeout=120)
        return DaytonaSandbox(sandbox, self._client, snapshot_name, self._snapshot_params)

    def ensure_snapshot(self, repo_id: str, repo_path_or_url: str) -> str:
        source_hash = _source_hash(repo_path_or_url)
        cached = _SNAPSHOT_IDS.get(repo_id)
        if cached is not None and cached[0] == source_hash:
            return cached[1]

        snapshot_name = _snapshot_name(repo_id, source_hash)
        with _snapshot_lock(repo_id, source_hash):
            cached = _SNAPSHOT_IDS.get(repo_id)
            if cached is not None and cached[0] == source_hash:
                return cached[1]

            try:
                snapshot = self._client.snapshot.get(snapshot_name)
            except Exception as exc:
                if getattr(exc, "status_code", None) != 404:
                    raise
            else:
                resolved_name = str(getattr(snapshot, "name", "") or "").strip()
                if not resolved_name:
                    raise RuntimeError("Daytona snapshot lookup returned no snapshot name")
                _SNAPSHOT_IDS[repo_id] = (source_hash, resolved_name)
                return resolved_name

            build_dir = Path(tempfile.mkdtemp(prefix="ripple-daytona-build-"))
            try:
                _materialize_repo(repo_path_or_url, build_dir / "repo")
                dockerfile = build_dir / "Dockerfile"
                dockerfile.write_text(_dockerfile(source_hash), encoding="utf-8")
                image = self._image_cls.from_dockerfile(dockerfile)
                params = self._create_snapshot_params(
                    name=snapshot_name,
                    image=image,
                )
                try:
                    snapshot = self._client.snapshot.create(params, timeout=900)
                except Exception as exc:
                    if getattr(exc, "status_code", None) != 409:
                        raise
                    snapshot = _snapshot_after_conflict(self._client, snapshot_name)
                    resolved_name = str(getattr(snapshot, "name", "") or "").strip()
                    if not resolved_name:
                        raise RuntimeError("Daytona snapshot lookup returned no snapshot name")
                else:
                    resolved_name = str(getattr(snapshot, "name", "") or "").strip()
                    if not resolved_name:
                        raise RuntimeError("Daytona image build returned no snapshot name")
                _SNAPSHOT_IDS[repo_id] = (source_hash, resolved_name)
                return resolved_name
            finally:
                shutil.rmtree(build_dir, ignore_errors=True)
