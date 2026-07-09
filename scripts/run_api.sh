#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"

export PYTHONPATH="${PWD}/services/api/src:${PWD}/core/src${PYTHONPATH:+:${PYTHONPATH}}"
exec "${PYTHON_BIN}" -m ripple_api
