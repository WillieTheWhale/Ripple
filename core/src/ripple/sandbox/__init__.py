"""Sandbox providers and verification helpers for RIPPLE fix-mode.

Set ``DAYTONA_API_KEY`` and install ``ripple[daytona]`` to select Daytona.
Without that environment variable, RIPPLE uses the local Docker provider.
"""

from __future__ import annotations

import os

from ripple.sandbox.base import RunResult, Sandbox, SandboxProvider
from ripple.sandbox.local_docker import LocalDockerProvider
from ripple.sandbox.verify import VerifyResult, scoped_tests, verify_patch


def provider_from_env() -> SandboxProvider:
    """Return the configured sandbox provider.

    Install ``ripple[daytona]`` and set ``DAYTONA_API_KEY`` to select Daytona.
    Otherwise, local Docker remains the default development implementation.
    """
    if os.environ.get("DAYTONA_API_KEY"):
        from ripple.sandbox.daytona_provider import DaytonaProvider

        return DaytonaProvider()
    return LocalDockerProvider()


__all__ = [
    "LocalDockerProvider",
    "RunResult",
    "Sandbox",
    "SandboxProvider",
    "VerifyResult",
    "provider_from_env",
    "scoped_tests",
    "verify_patch",
]
