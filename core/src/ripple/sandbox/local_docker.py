"""Local Docker implementation of the RIPPLE sandbox provider."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from ripple.sandbox.base import RunResult

logger = logging.getLogger(__name__)

_OUTPUT_LIMIT: Final[int] = 20_000
_ORPHAN_REAP_GRACE_SECONDS: Final[int] = 15 * 60
_SANDBOX_LABEL: Final[str] = "ripple.sandbox"
_SANDBOX_LABEL_VALUE: Final[str] = "1"
_SANDBOX_OWNER_PID_LABEL: Final[str] = "ripple.owner_pid"
_IGNORED_NAMES: Final[set[str]] = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
}


@dataclass(frozen=True)
class OrphanReapResult:
    """Outcome of one local Docker orphan-reaping pass.

    Container names are reported without Docker's leading slash so callers can
    log them or correlate them with their sandbox registry.
    """

    removed: tuple[str, ...] = ()
    skipped_active: tuple[str, ...] = ()
    skipped_recent: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


class LocalDockerSandbox:
    """A running Docker container with a repository checkout at ``/work/repo``."""

    def __init__(self, container_name: str, image: str, repo_id: str, output_limit: int = _OUTPUT_LIMIT) -> None:
        self.container_name = container_name
        self.image = image
        self.repo_id = repo_id
        self.output_limit = output_limit

    def upload_file(self, path: str, content: str) -> None:
        """Upload text content to *path* inside the container."""
        parent = _quote(str(Path(path).parent))
        _run(
            ["docker", "exec", self.container_name, "sh", "-lc", f"mkdir -p {parent}"],
            timeout=15,
        )
        quoted_path = _quote(path)
        result = subprocess.run(
            ["docker", "exec", "-i", self.container_name, "sh", "-lc", f"cat > {quoted_path}"],
            input=content,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            env=_docker_env(),
        )
        if result.returncode != 0:
            raise RuntimeError(_combine_output(result))

    def download_file(self, path: str) -> str:
        """Download text content from *path* inside the container."""
        result = _run(
            ["docker", "exec", self.container_name, "cat", path],
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise FileNotFoundError(_combine_output(result))
        return result.stdout

    def reset(self) -> None:
        """Recreate the container from its immutable image baseline."""
        _run(["docker", "rm", "-f", self.container_name], timeout=30, check=False)
        _start_container(self.image, self.container_name, self.repo_id)

    def run_command(
        self,
        cmd: str,
        cwd: str | None = None,
        timeout: int = 300,
    ) -> RunResult:
        """Run *cmd* in the container and return combined stdout/stderr."""
        args = ["docker", "exec", "-i"]
        if cwd:
            args.extend(["-w", cwd])
        args.extend([self.container_name, "sh", "-lc", cmd])
        return _run_bounded(args, timeout=timeout, output_limit=self.output_limit)

    def destroy(self) -> None:
        """Remove the backing Docker container."""
        _run(["docker", "rm", "-f", self.container_name], timeout=30, check=False)


class LocalDockerProvider:
    """Build repository snapshots and create local Docker sandboxes."""

    def create(self, repo_id: str) -> LocalDockerSandbox:
        """Run a disposable container from the repository snapshot image."""
        image = _image_name(repo_id)
        name = f"ripple-sbx-run-{uuid.uuid4().hex[:8]}"
        _start_container(image, name, repo_id)
        return LocalDockerSandbox(name, image, repo_id)

    def ensure_snapshot(self, repo_id: str, repo_path_or_url: str) -> str:
        """Build or reuse the Docker image snapshot for *repo_id*."""
        image = _image_name(repo_id)
        source_hash = _source_hash(repo_path_or_url)
        current_hash = _image_label(image, "org.ripple.source_hash")
        if current_hash == source_hash:
            logger.info("Reusing sandbox snapshot %s", image)
            return image

        logger.info("Building sandbox snapshot %s from %s", image, repo_path_or_url)
        build_dir = Path(tempfile.mkdtemp(prefix="ripple-sbx-build-"))
        try:
            repo_dir = build_dir / "repo"
            _materialize_repo(repo_path_or_url, repo_dir)
            (build_dir / "Dockerfile").write_text(_dockerfile(source_hash), encoding="utf-8")
            _run(["docker", "build", "-t", image, str(build_dir)], timeout=900)
        finally:
            shutil.rmtree(build_dir, ignore_errors=True)
        return image


def reap_orphaned_local_docker_sandboxes(
    active_container_refs: Collection[str],
    *,
    min_age_seconds: float = _ORPHAN_REAP_GRACE_SECONDS,
) -> OrphanReapResult:
    """Remove stale RIPPLE Docker sandboxes not present in the live registry.

    ``active_container_refs`` must be a snapshot of every locally active
    sandbox's Docker name or ID, collected while the orchestrator protects its
    sandbox registry. This function only considers containers with the exact
    ``ripple.sandbox=1`` label, skips active references and live owner PIDs,
    and requires a positive grace period before removal. It is therefore safe
    to call at startup with an empty registry and periodically with the current
    registry.
    """
    if min_age_seconds <= 0:
        raise ValueError("min_age_seconds must be positive")

    active_refs = _normalized_container_refs(active_container_refs)
    listed = _run(
        [
            "docker",
            "container",
            "ls",
            "-aq",
            "--filter",
            f"label={_SANDBOX_LABEL}={_SANDBOX_LABEL_VALUE}",
        ],
        timeout=30,
        check=False,
    )
    if listed.returncode != 0:
        logger.warning("Unable to list local Docker sandboxes for orphan reaping: %s", _combine_output(listed))
        return OrphanReapResult()

    removed: list[str] = []
    skipped_active: list[str] = []
    skipped_recent: list[str] = []
    failed: list[str] = []
    cutoff = time.time() - min_age_seconds

    for container_id in listed.stdout.splitlines():
        container_id = container_id.strip()
        if not container_id:
            continue
        inspected = _run(
            ["docker", "container", "inspect", container_id],
            timeout=30,
            check=False,
        )
        if inspected.returncode != 0:
            logger.debug("Skipping Docker container %s during orphan reaping: inspect failed", container_id)
            continue
        container = _inspected_container(inspected.stdout)
        if container is None or not _is_ripple_sandbox(container):
            continue

        name = _container_name(container, container_id)
        if _container_refs(container, name).intersection(active_refs) or _has_live_owner(container):
            skipped_active.append(name)
            continue

        created_at = _container_created_at(container)
        if created_at is None or created_at > cutoff:
            skipped_recent.append(name)
            continue

        deleted = _run(
            ["docker", "rm", "-f", str(container.get("Id") or container_id)],
            timeout=30,
            check=False,
        )
        if deleted.returncode == 0:
            removed.append(name)
            logger.info("Removed stale local Docker sandbox %s", name)
        else:
            failed.append(name)
            logger.warning("Unable to remove stale local Docker sandbox %s: %s", name, _combine_output(deleted))

    return OrphanReapResult(
        removed=tuple(removed),
        skipped_active=tuple(skipped_active),
        skipped_recent=tuple(skipped_recent),
        failed=tuple(failed),
    )


def _normalized_container_refs(container_refs: Collection[str]) -> set[str]:
    return {reference.lstrip("/") for reference in container_refs if reference}


def _inspected_container(output: str) -> dict[str, object] | None:
    try:
        containers = json.loads(output)
    except json.JSONDecodeError:
        logger.warning("Docker returned invalid container inspection data during orphan reaping")
        return None
    if not isinstance(containers, list) or len(containers) != 1 or not isinstance(containers[0], dict):
        logger.warning("Docker returned unexpected container inspection data during orphan reaping")
        return None
    return containers[0]


def _is_ripple_sandbox(container: dict[str, object]) -> bool:
    config = container.get("Config")
    if not isinstance(config, dict):
        return False
    labels = config.get("Labels")
    return isinstance(labels, dict) and labels.get(_SANDBOX_LABEL) == _SANDBOX_LABEL_VALUE


def _has_live_owner(container: dict[str, object]) -> bool:
    """Return true when another live host process may own this container."""
    config = container.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    owner = labels.get(_SANDBOX_OWNER_PID_LABEL) if isinstance(labels, dict) else None
    try:
        owner_pid = int(owner)
    except (TypeError, ValueError):
        state = container.get("State")
        # Legacy running containers have no ownership label, so preserve them.
        return not isinstance(state, dict) or bool(state.get("Running", True))
    if owner_pid <= 0:
        return True
    return _pid_is_alive(owner_pid)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _container_name(container: dict[str, object], fallback: str) -> str:
    name = container.get("Name")
    return name.lstrip("/") if isinstance(name, str) and name else fallback


def _container_refs(container: dict[str, object], name: str) -> set[str]:
    container_id = container.get("Id")
    refs = {name}
    if isinstance(container_id, str) and container_id:
        refs.add(container_id)
        refs.add(container_id[:12])
    return refs


def _container_created_at(container: dict[str, object]) -> float | None:
    created = container.get("Created")
    if not isinstance(created, str) or not created:
        logger.warning("Skipping Docker container without a creation timestamp during orphan reaping")
        return None
    try:
        return datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    except ValueError:
        logger.warning("Skipping Docker container with an invalid creation timestamp during orphan reaping")
        return None


def _start_container(image: str, name: str, repo_id: str) -> None:
    """Start a resource-bounded, network-isolated sandbox container."""
    _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "none",
            "--cpus",
            os.environ.get("RIPPLE_SANDBOX_CPUS", "2"),
            "--memory",
            os.environ.get("RIPPLE_SANDBOX_MEMORY", "2g"),
            "--pids-limit",
            os.environ.get("RIPPLE_SANDBOX_PIDS", "256"),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--label",
            "ripple.sandbox=1",
            "--label",
            f"ripple.repo_id={repo_id}",
            "--label",
            f"{_SANDBOX_OWNER_PID_LABEL}={os.getpid()}",
            image,
            "sleep",
            "infinity",
        ],
        timeout=60,
    )


def _materialize_repo(repo_path_or_url: str, dest: Path) -> None:
    source = Path(repo_path_or_url).expanduser()
    if _is_git_url(repo_path_or_url):
        _run(["git", "clone", "--depth", "1", repo_path_or_url, str(dest)], timeout=300)
        shutil.rmtree(dest / ".git", ignore_errors=True)
        return
    if not source.exists() or not source.is_dir():
        raise FileNotFoundError(f"Repository path does not exist: {repo_path_or_url}")
    shutil.copytree(source.resolve(), dest, ignore=_copy_ignore)


def _dockerfile(source_hash: str) -> str:
    return f"""FROM python:3.12-slim
LABEL org.ripple.source_hash="{source_hash}"
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1
RUN apt-get update \\
    && apt-get install -y --no-install-recommends git build-essential \\
    && rm -rf /var/lib/apt/lists/*
WORKDIR /work/repo
COPY repo/ /work/repo/
RUN python -m pip install --upgrade pip setuptools wheel
RUN python -m pip install pytest coverage pytest-timeout
RUN set -eux; \\
    installed=0; \\
    if [ -f pyproject.toml ] || [ -f setup.py ] || [ -f setup.cfg ]; then \\
      python -m pip install -e '.[test,dev]' && installed=1 || true; \\
      if [ "$installed" = "0" ]; then python -m pip install -e '.[test]' && installed=1 || true; fi; \\
      if [ "$installed" = "0" ]; then python -m pip install -e '.[dev]' && installed=1 || true; fi; \\
      if [ "$installed" = "0" ]; then python -m pip install -e . && installed=1 || true; fi; \\
    fi; \\
    if [ "$installed" = "0" ]; then \\
      reqs="$(ls requirements*.txt 2>/dev/null || true)"; \\
      if [ -n "$reqs" ]; then for req in $reqs; do python -m pip install -r "$req"; done; fi; \\
    fi
RUN git init \\
    && git config user.email ripple-sandbox@example.invalid \\
    && git config user.name "RIPPLE Sandbox" \\
    && git add . \\
    && git commit -m "snapshot baseline" >/dev/null
RUN python -m pytest --collect-only -q -p no:cacheprovider || true
"""


def _source_hash(repo_path_or_url: str) -> str:
    source = Path(repo_path_or_url).expanduser()
    if _is_git_url(repo_path_or_url) or not source.exists():
        return hashlib.sha256(repo_path_or_url.encode("utf-8")).hexdigest()

    digest = hashlib.sha256()
    root = source.resolve()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in _IGNORED_NAMES for part in rel.parts):
            continue
        digest.update(str(rel).replace("\\", "/").encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _image_label(image: str, label: str) -> str | None:
    template = '{{ index .Config.Labels "' + label + '" }}'
    result = _run(
        ["docker", "image", "inspect", "-f", template, image],
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value if value and value != "<no value>" else None


def _image_name(repo_id: str) -> str:
    slug = re.sub(r"[^a-z0-9_.-]+", "-", repo_id.lower()).strip("-")
    return f"ripple-sbx-{slug or 'repo'}"


def _is_git_url(value: str) -> bool:
    return "://" in value or value.startswith("git@") or (value.endswith(".git") and not Path(value).exists())


def _copy_ignore(_dir: str, names: list[str]) -> set[str]:
    ignored = {name for name in names if name in _IGNORED_NAMES}
    ignored.update(name for name in names if name.endswith(".pyc"))
    return ignored


def _run(
    args: list[str],
    *,
    timeout: int,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        env=_docker_env(),
    )
    if check and result.returncode != 0:
        raise RuntimeError(_combine_output(result))
    return result


def _run_bounded(args: list[str], *, timeout: int, output_limit: int) -> RunResult:
    """Run a command while spooling output to disk instead of host memory."""
    with tempfile.TemporaryFile(mode="w+b") as output:
        try:
            result = subprocess.run(
                args,
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
                env=_docker_env(),
            )
            exit_code = int(result.returncode)
        except subprocess.TimeoutExpired:
            exit_code = 124

        size = output.tell()
        truncated = size > output_limit
        output.seek(max(0, size - output_limit))
        text = output.read().decode("utf-8", errors="replace")
    return {"exit_code": exit_code, "output": text, "truncated": truncated}


def _combine_output(result: subprocess.CompletedProcess[str]) -> str:
    return _as_text(result.stdout) + _as_text(result.stderr)


def _as_text(value: str | bytes | None) -> str:
    """Normalize subprocess output, which may be bytes after a timeout."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _tail(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[-limit:], True


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _docker_env() -> dict[str, str]:
    env = dict(os.environ)
    if not env.get("DOCKER_CONFIG"):
        config_dir = Path(
            env.get("RIPPLE_DOCKER_CONFIG", str(Path(tempfile.gettempdir()) / "ripple-docker-config"))
        )
        config_dir.mkdir(parents=True, exist_ok=True)
        env["DOCKER_CONFIG"] = str(config_dir)
    return env
