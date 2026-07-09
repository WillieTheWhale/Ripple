#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"

export PYTHONPATH="${PWD}/services/ripple-mcp/src:${PWD}/core/src:${PWD}/axon/src${PYTHONPATH:+:${PYTHONPATH}}"
exec "${PYTHON_BIN}" -m ripple_mcp
