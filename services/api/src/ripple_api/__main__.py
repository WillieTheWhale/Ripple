"""Run the RIPPLE API with ``python -m ripple_api``."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    """Start the local development API server."""
    host = os.environ.get("RIPPLE_API_HOST", "127.0.0.1")
    port = int(os.environ.get("RIPPLE_API_PORT", "8787"))
    uvicorn.run("ripple_api.app:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
