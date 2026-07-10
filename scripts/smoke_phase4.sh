#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
MINI_REPO_ID="${MINI_REPO_ID:-miniproj}"
NEO4J_CONTAINER="${NEO4J_CONTAINER:-ripple-neo4j}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
NEO4J_PASSWORD="${NEO4J_PASSWORD:-ripplepass}"
NEO4J_DATABASE="${NEO4J_DATABASE:-neo4j}"
ROCKETRIDE_URL="${ROCKETRIDE_HTTP_URL:-http://localhost:5565}"
MCP_HOST="${RIPPLE_MCP_HOST:-127.0.0.1}"
MCP_PORT="${RIPPLE_MCP_PORT:-}"
MCP_LAUNCHD_LABEL="${RIPPLE_MCP_LAUNCHD_LABEL:-local.ripple.phase4.mcp}"
OLD_MCP_LAUNCHD_LABEL="${RIPPLE_OLD_MCP_LAUNCHD_LABEL:-local.ripple.phase3.mcp}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# Smoke tests must never publish externally, even when the parent shell is PR-enabled.
unset RIPPLE_PR_REPO

export PYTHONPATH="${PWD}/services/gateway/src:${PWD}/services/ripple-mcp/src:${PWD}/core/src:${PWD}/axon/src${PYTHONPATH:+:${PYTHONPATH}}"

choose_mcp_port() {
  local candidate
  if [[ -n "${MCP_PORT}" ]]; then
    return 0
  fi

  for candidate in {8791..8891}; do
    if "${PYTHON_BIN}" - "${MCP_HOST}" "${candidate}" <<'PY' >/dev/null 2>&1
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind((host, port))
PY
    then
      MCP_PORT="${candidate}"
      return 0
    fi
  done

  echo "Could not find a free RIPPLE MCP port in 8791-8891" >&2
  exit 1
}

choose_mcp_port
export NEO4J_USER NEO4J_PASSWORD NEO4J_DATABASE MINI_REPO_ID
export NEO4J_URI="${RIPPLE_PHASE4_NEO4J_URI:-${NEO4J_URI:-bolt://localhost:7687}}"
export RIPPLE_MCP_HOST="${MCP_HOST}"
export RIPPLE_MCP_PORT="${MCP_PORT}"
export RIPPLE_MCP_ENDPOINT="http://${MCP_HOST}:${MCP_PORT}/mcp"
export RIPPLE_DOCKER_CONFIG="${RIPPLE_DOCKER_CONFIG:-${TMPDIR:-/tmp}/ripple-docker-config}"
export DOCKER_CONFIG="${DOCKER_CONFIG:-${RIPPLE_DOCKER_CONFIG}}"
mkdir -p "${DOCKER_CONFIG}"

PHASE4_TMP_DIR="$(mktemp -d -t ripple-phase4.XXXXXX)"
INGEST_JSON="${PHASE4_TMP_DIR}/ingest.json"
DETERMINISTIC_JSON="${PHASE4_TMP_DIR}/deterministic.json"
GATEWAY_UP_JSON="${PHASE4_TMP_DIR}/gateway-up.json"
FIX_JSON="${PHASE4_TMP_DIR}/fix.json"
CROSSCHECK_JSON="${PHASE4_TMP_DIR}/crosscheck.json"
ENGINE_LOG="${PWD}/infra/rocketride/engine.phase4.log"
MCP_LOG="${PWD}/.ripple_mcp.phase4.log"
MINI_REPO="${PWD}/core/tests/fixtures/miniproj"

cleanup_sandboxes() {
  local containers
  containers="$(docker ps -aq --filter "name=ripple-sbx-run-" 2>/dev/null || true)"
  if [[ -n "${containers}" ]]; then
    docker rm -f ${containers} >/dev/null 2>&1 || true
  fi
}

cleanup() {
  local status=$?
  "${PYTHON_BIN}" -m ripple_gateway down >/dev/null 2>&1 || true
  stop_mcp
  cleanup_sandboxes
  if [[ "${status}" -ne 0 ]]; then
    echo "Phase 4 smoke failed. Recent logs:" >&2
    if [[ -f "${ENGINE_LOG}" ]]; then
      echo "--- engine log ---" >&2
      tail -80 "${ENGINE_LOG}" >&2 || true
    fi
    if [[ -f "${MCP_LOG}" ]]; then
      echo "--- mcp log ---" >&2
      tail -120 "${MCP_LOG}" >&2 || true
    fi
  fi
  rm -rf "${PHASE4_TMP_DIR}"
  exit "${status}"
}
trap cleanup EXIT

cypher() {
  docker exec "${NEO4J_CONTAINER}" cypher-shell \
    -u "${NEO4J_USER}" \
    -p "${NEO4J_PASSWORD}" \
    -d "${NEO4J_DATABASE}" \
    --format plain \
    "$1"
}

count_query() {
  cypher "$1" | grep -Eo '^[0-9]+$' | tail -1
}

ensure_neo4j() {
  if ! docker inspect "${NEO4J_CONTAINER}" >/dev/null 2>&1; then
    echo "Neo4j container ${NEO4J_CONTAINER} does not exist" >&2
    exit 1
  fi
  if ! docker ps --format '{{.Names}}' | grep -qx "${NEO4J_CONTAINER}"; then
    docker start "${NEO4J_CONTAINER}" >/dev/null
  fi
  if "${PYTHON_BIN}" - <<'PY'
import os
import time

from neo4j import GraphDatabase

uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
user = os.environ.get("NEO4J_USER", "neo4j")
password = os.environ.get("NEO4J_PASSWORD", "ripplepass")
deadline = time.monotonic() + 120
last_error: Exception | None = None

while time.monotonic() < deadline:
    driver = GraphDatabase.driver(uri, auth=(user, password), connection_timeout=1)
    try:
        driver.verify_connectivity()
        print("Neo4j: up")
        raise SystemExit(0)
    except Exception as exc:
        last_error = exc
    finally:
        driver.close()
    time.sleep(0.5)

print(f"Neo4j host connection did not become queryable: {last_error}", file=__import__("sys").stderr)
raise SystemExit(1)
PY
  then
    return 0
  fi
  exit 1
}

ensure_engine() {
  if curl -fsS "${ROCKETRIDE_URL}/version" \
      -H "Authorization: Bearer ${ROCKETRIDE_APIKEY:-ripple-local-dev-key}" >/dev/null 2>&1; then
    echo "RocketRide engine: up"
    return 0
  fi

  echo "Starting RocketRide engine"
  (
    cd infra/rocketride
    ROCKETRIDE_APIKEY="${ROCKETRIDE_APIKEY:-ripple-local-dev-key}" \
      nohup ./engine ./ai/eaas.py --host=127.0.0.1 >"${ENGINE_LOG}" 2>&1 < /dev/null &
    engine_pid=$!
    echo "${engine_pid}" > .engine.phase4.pid
    disown "${engine_pid}" 2>/dev/null || true
  )

  for _ in {1..160}; do
    if curl -fsS "${ROCKETRIDE_URL}/version" \
        -H "Authorization: Bearer ${ROCKETRIDE_APIKEY:-ripple-local-dev-key}" >/dev/null 2>&1; then
      echo "RocketRide engine: started"
      return 0
    fi
    sleep 0.5
  done
  echo "RocketRide engine did not become healthy" >&2
  exit 1
}

restart_mcp() {
  echo "Starting fresh RIPPLE MCP"
  if command -v launchctl >/dev/null 2>&1; then
    launchctl remove "${MCP_LAUNCHD_LABEL}" >/dev/null 2>&1 || true
    launchctl remove "${OLD_MCP_LAUNCHD_LABEL}" >/dev/null 2>&1 || true
  fi
  if [[ -f .ripple_mcp.phase3.pid ]]; then
    old_pid="$(cat .ripple_mcp.phase3.pid 2>/dev/null || true)"
    if [[ "${old_pid}" =~ ^[0-9]+$ ]]; then
      kill "${old_pid}" >/dev/null 2>&1 || true
    fi
  fi
  if [[ -f .ripple_mcp.phase4.pid ]]; then
    old_pid="$(cat .ripple_mcp.phase4.pid 2>/dev/null || true)"
    if [[ "${old_pid}" =~ ^[0-9]+$ ]]; then
      kill "${old_pid}" >/dev/null 2>&1 || true
    fi
  fi
  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -tiTCP:${MCP_PORT} -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -n "${pids}" ]]; then
      kill ${pids} >/dev/null 2>&1 || true
      for _ in {1..20}; do
        remaining="$(lsof -tiTCP:${MCP_PORT} -sTCP:LISTEN 2>/dev/null || true)"
        if [[ -z "${remaining}" ]]; then
          break
        fi
        sleep 0.25
      done
      remaining="$(lsof -tiTCP:${MCP_PORT} -sTCP:LISTEN 2>/dev/null || true)"
      if [[ -n "${remaining}" ]]; then
        kill -9 ${remaining} >/dev/null 2>&1 || true
      fi
    fi
  fi

  nohup scripts/run_mcp.sh >"${MCP_LOG}" 2>&1 < /dev/null &
  mcp_pid=$!
  echo "${mcp_pid}" > .ripple_mcp.phase4.pid
  for _ in {1..120}; do
    if ! kill -0 "${mcp_pid}" >/dev/null 2>&1; then
      echo "RIPPLE MCP exited before opening ${MCP_HOST}:${MCP_PORT}" >&2
      tail -80 "${MCP_LOG}" >&2 || true
      return 1
    fi
    if "${PYTHON_BIN}" - "${MCP_HOST}" "${MCP_PORT}" <<'PY' >/dev/null 2>&1
import socket
import sys

with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=0.25):
    pass
PY
    then
      echo "RIPPLE MCP: started on ${MCP_HOST}:${MCP_PORT}"
      return 0
    fi
    sleep 0.25
  done
  echo "RIPPLE MCP did not open ${MCP_HOST}:${MCP_PORT}" >&2
  return 1
}

stop_mcp() {
  local pid
  if [[ -f .ripple_mcp.phase4.pid ]]; then
    pid="$(cat .ripple_mcp.phase4.pid 2>/dev/null || true)"
    if [[ "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" >/dev/null 2>&1; then
      kill "${pid}" >/dev/null 2>&1 || true
      wait "${pid}" 2>/dev/null || true
    fi
    rm -f .ripple_mcp.phase4.pid
  fi
}

assert_no_sandboxes() {
  local leaked
  leaked="$(docker ps -aq --filter "name=ripple-sbx-run-" 2>/dev/null || true)"
  if [[ -n "${leaked}" ]]; then
    echo "Leaked sandbox containers:" >&2
    docker ps -a --filter "name=ripple-sbx-run-" >&2 || true
    exit 1
  fi
  echo "Sandbox cleanup: no leaked ripple-sbx-* containers"
}

command -v docker >/dev/null
docker info >/dev/null

ensure_neo4j

"${PYTHON_BIN}" -m ripple.ingest \
  --repo "${MINI_REPO}" \
  --repo-id "${MINI_REPO_ID}" \
  --wipe >"${INGEST_JSON}"

mini_functions="$(count_query "MATCH (f:Function {repo_id: '${MINI_REPO_ID}'}) RETURN count(f) AS count")"
mini_root="$(cypher "MATCH (repo:Repo {repo_id: '${MINI_REPO_ID}'}) RETURN repo.root_path AS root_path" | grep -F "${MINI_REPO}" | tail -1 || true)"
echo "P1 ingest: ${MINI_REPO_ID} functions=${mini_functions}"
if [[ -z "${mini_functions}" || "${mini_functions}" -le 0 || -z "${mini_root}" ]]; then
  echo "Expected Function nodes and Repo.root_path for ${MINI_REPO_ID}" >&2
  exit 1
fi

"${PYTHON_BIN}" - "${MINI_REPO_ID}" "${MINI_REPO}" >"${DETERMINISTIC_JSON}" <<'PY'
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import asdict

from ripple.db import create_driver
from ripple.sandbox.local_docker import LocalDockerProvider, reap_orphaned_local_docker_sandboxes
from ripple.sandbox.verify import scoped_tests, verify_patch

repo_id = sys.argv[1]
repo_path = sys.argv[2]
fqn = "src/miniproj/service.py:compute_value"

good_diff = """diff --git a/src/miniproj/service.py b/src/miniproj/service.py
--- a/src/miniproj/service.py
+++ b/src/miniproj/service.py
@@ -14,6 +14,6 @@ class Processor:
 def compute_value(value: int) -> int:
     \"\"\"Process a value.\"\"\"
-    return normalize(value)
+    return normalize(value) + 1


 def compute_label(value: int) -> str:
diff --git a/tests/test_mini.py b/tests/test_mini.py
--- a/tests/test_mini.py
+++ b/tests/test_mini.py
@@ -5,5 +5,5 @@ from miniproj.service import compute_label, compute_value
 def test_compute_value() -> None:
-    assert compute_value(3) == normalize(3)
+    assert compute_value(3) == normalize(3) + 1


 def test_label() -> None:
"""

bad_diff = """diff --git a/src/miniproj/service.py b/src/miniproj/service.py
--- a/src/miniproj/service.py
+++ b/src/miniproj/service.py
@@ -14,6 +14,6 @@ class Processor:
 def compute_value(value: int) -> int:
     \"\"\"Process a value.\"\"\"
-    return normalize(value)
+    return normalize(value) + 1


 def compute_label(value: int) -> str:
"""

driver = create_driver()
provider = LocalDockerProvider()
snapshot = provider.ensure_snapshot(repo_id, repo_path)
tests = scoped_tests(driver, repo_id, [fqn])
assert tests == ["tests/test_mini.py"], tests

# Simulate an ungraceful owner exit and prove the real Docker resource is reaped.
child = subprocess.run(
    [
        sys.executable,
        "-c",
        (
            "from ripple.sandbox.local_docker import LocalDockerProvider; "
            f"print(LocalDockerProvider().create({repo_id!r}).container_name)"
        ),
    ],
    text=True,
    capture_output=True,
    check=True,
)
orphan_name = child.stdout.strip().splitlines()[-1]
time.sleep(0.05)
reaped = reap_orphaned_local_docker_sandboxes(set(), min_age_seconds=0.01)
assert orphan_name in reaped.removed, {"orphan": orphan_name, "reaped": asdict(reaped)}


def run_diff(diff: str, expected: bool):
    sandbox = provider.create(repo_id)
    try:
        result = verify_patch(sandbox, diff, tests)
        assert result.passed is expected, asdict(result)
        return result
    finally:
        sandbox.destroy()


good = run_diff(good_diff, True)
bad = run_diff(bad_diff, False)
print(json.dumps({
    "snapshot": snapshot,
    "orphan_reaped": orphan_name,
    "scoped_tests": tests,
    "good_diff": good_diff,
    "good": asdict(good),
    "bad": asdict(bad),
}, sort_keys=True))
PY

"${PYTHON_BIN}" - "${DETERMINISTIC_JSON}" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text())
assert payload["good"]["passed"] is True, payload
assert payload["bad"]["passed"] is False, payload
print("Deterministic verify:", {
    "scoped_tests": payload["scoped_tests"],
    "good_passed": payload["good"]["passed"],
    "bad_passed": payload["bad"]["passed"],
})
PY

ensure_engine
restart_mcp

"${PYTHON_BIN}" - "${RIPPLE_MCP_ENDPOINT}" "${MINI_REPO_ID}" "${DETERMINISTIC_JSON}" <<'PY'
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main() -> None:
    endpoint, repo_id, deterministic_path = sys.argv[1:]
    deterministic = json.loads(Path(deterministic_path).read_text())
    good_diff = deterministic["good_diff"]
    fqn = "src/miniproj/service.py:compute_value"

    async with streamable_http_client(endpoint) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                response = await session.call_tool(name, arguments)
                if response.isError:
                    raise AssertionError({"tool": name, "content": response.content})
                if isinstance(response.structuredContent, dict):
                    return response.structuredContent
                for item in response.content:
                    text = getattr(item, "text", None)
                    if isinstance(text, str):
                        payload = json.loads(text)
                        if isinstance(payload, dict):
                            return payload
                raise AssertionError({"tool": name, "content": response.content})

            async def wait_job(job_id: str, timeout: float = 180) -> dict[str, Any]:
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    record = await call("job_status", {"job_id": job_id})
                    if record.get("status") == "done":
                        return record
                    if record.get("status") == "failed":
                        raise AssertionError(record)
                    await asyncio.sleep(0.25)
                raise AssertionError(f"MCP job timed out: {job_id}")

            prepared = await call("prepare_sandbox", {"repo_id": repo_id})
            prepare_job = await wait_job(prepared["job_id"])
            sandbox_id = prepare_job["result"]["sandbox_id"]

            queued = await call(
                "verify_patch",
                {
                    "request_id": "phase4-deterministic-request",
                    "repo_id": repo_id,
                    "sandbox_id": sandbox_id,
                    "diff_text": good_diff,
                    "fqns": [fqn],
                },
            )
            verify_job = await wait_job(queued["job_id"])
            verified = verify_job["result"]
            assert verified["passed"] is True, verified

            pr = await call(
                "open_draft_pr",
                {
                    "repo_id": repo_id,
                    "verify_job_id": queued["job_id"],
                    "branch_name": "ripple/fix-smoke",
                    "diff_text": verified["diff"],
                    "title": "RIPPLE phase 4 smoke",
                    "body": "Proof-gated smoke test",
                },
            )
            assert pr.get("skipped") is True, pr

            finalized = await call(
                "finalize_fix_result",
                {
                    "request_id": "phase4-deterministic-request",
                    "verify_job_id": queued["job_id"],
                    "pr_job_id": None,
                    "impact": [],
                    "cypher_used": [],
                    "iterations": 1,
                },
            )
            assert finalized["verification"]["passed"] is True, finalized
            assert finalized["diff"] == verified["diff"], finalized
            recovered = await call(
                "get_finalized_fix_result",
                {"request_id": "phase4-deterministic-request"},
            )
            assert recovered == finalized, {"finalized": finalized, "recovered": recovered}
            print("Live MCP verify:", {
                "passed": True,
                "pr_skipped": pr["skipped"],
                "sandbox_id": sandbox_id,
            })


asyncio.run(main())
PY

if [[ "${RIPPLE_SKIP_AGENT_SMOKE:-0}" == "1" ]]; then
  "${PYTHON_BIN}" -m ripple_gateway down >/dev/null 2>&1 || true
  stop_mcp
  assert_no_sandboxes
  echo "Phase 4 deterministic and live MCP smoke passed (agent layer skipped)"
  exit 0
fi

"${PYTHON_BIN}" -m ripple_gateway down >/dev/null 2>&1 || true
"${PYTHON_BIN}" -m ripple_gateway up >"${GATEWAY_UP_JSON}"
"${PYTHON_BIN}" - "${GATEWAY_UP_JSON}" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text())
assert payload.get("p2", {}).get("token"), payload
print("Gateway up:", {"p2": "ready"})
PY

"${PYTHON_BIN}" -m ripple_gateway fix \
  --repo-id "${MINI_REPO_ID}" \
  --question "For src/miniproj/service.py:compute_label, change only its docstring from 'Process and format a value.' to 'Format a processed value.' Preserve behavior and do not edit tests. The exact function is: def compute_label(value: int) -> str: then the docstring, then return format_value(value). Use a complete unified diff with that return line and no ellipses. Inspect the exact function source before drafting. Return the canonical verified diff." \
  --timeout "${RIPPLE_PHASE4_AGENT_TIMEOUT:-480}" >"${FIX_JSON}"

"${PYTHON_BIN}" - "${FIX_JSON}" <<'PY'
import json
import sys
from pathlib import Path

answer = json.loads(Path(sys.argv[1]).read_text())
assert answer.get("mode") == "fix", answer
assert isinstance(answer.get("diff"), str) and answer["diff"].strip(), answer
verification = answer.get("verification")
assert isinstance(verification, dict), answer
assert verification.get("passed") is True, answer
iterations = answer.get("iterations")
assert isinstance(iterations, int) and iterations <= 3, answer
print("P2 fix:", {
    "passed": verification["passed"],
    "iterations": iterations,
    "diff_chars": len(answer["diff"]),
    "pr_url": answer.get("pr_url"),
})
PY

"${PYTHON_BIN}" - "${MINI_REPO_ID}" "${MINI_REPO}" "${FIX_JSON}" >"${CROSSCHECK_JSON}" <<'PY'
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

from ripple.sandbox.local_docker import LocalDockerProvider
from ripple.sandbox.verify import verify_patch

repo_id = sys.argv[1]
repo_path = sys.argv[2]
answer = json.loads(Path(sys.argv[3]).read_text())
diff = answer["diff"]
tests = ["tests/test_mini.py"]

provider = LocalDockerProvider()
provider.ensure_snapshot(repo_id, repo_path)
sandbox = provider.create(repo_id)
try:
    result = verify_patch(sandbox, diff, tests)
    assert result.passed is True, {"result": asdict(result), "diff": diff}
    print(json.dumps(asdict(result), sort_keys=True))
finally:
    sandbox.destroy()
PY

"${PYTHON_BIN}" - "${CROSSCHECK_JSON}" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text())
print("Direct cross-check:", {"passed": payload["passed"], "cmd": payload["cmd"]})
PY

"${PYTHON_BIN}" -m ripple_gateway down >/dev/null
stop_mcp
assert_no_sandboxes
echo "Phase 4 smoke passed"
