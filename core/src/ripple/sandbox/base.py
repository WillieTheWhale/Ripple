"""Sandbox provider contracts used by RIPPLE verification."""

from __future__ import annotations

from typing import Protocol, TypedDict


class RunResult(TypedDict):
    """Result returned by sandbox command execution."""

    exit_code: int
    output: str
    truncated: bool


class Sandbox(Protocol):
    """A disposable repository workspace with Daytona-shaped operations."""

    def upload_file(self, path: str, content: str) -> None:
        """Upload text content to an absolute path inside the sandbox."""

    def download_file(self, path: str) -> str:
        """Download a text file from an absolute path inside the sandbox."""

    def reset(self) -> None:
        """Restore the immutable repository snapshot baseline."""

    def run_command(
        self,
        cmd: str,
        cwd: str | None = None,
        timeout: int = 300,
    ) -> RunResult:
        """Run a shell command inside the sandbox."""

    def destroy(self) -> None:
        """Destroy the sandbox and release provider resources."""


class SandboxProvider(Protocol):
    """Factory for repository snapshots and sandbox instances."""

    def create(self, repo_id: str) -> Sandbox:
        """Create a sandbox from a previously ensured repository snapshot."""

    def ensure_snapshot(self, repo_id: str, repo_path_or_url: str) -> str:
        """Ensure a prebuilt snapshot image exists and return its provider id."""
