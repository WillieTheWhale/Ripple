#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
REPO_ID="${REPO_ID:-axon-self}"
MINI_REPO_ID="${MINI_REPO_ID:-miniproj}"
NEO4J_CONTAINER="${NEO4J_CONTAINER:-ripple-neo4j}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
NEO4J_PASSWORD="${NEO4J_PASSWORD:-ripplepass}"
NEO4J_DATABASE="${NEO4J_DATABASE:-neo4j}"
ROCKETRIDE_URL="${ROCKETRIDE_HTTP_URL:-http://localhost:5565}"
MCP_HOST="${RIPPLE_MCP_HOST:-127.0.0.1}"
MCP_PORT="${RIPPLE_MCP_PORT:-8790}"
MCP_LAUNCHD_LABEL="${RIPPLE_MCP_LAUNCHD_LABEL:-local.ripple.phase3.mcp}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export PYTHONPATH="${PWD}/services/gateway/src:${PWD}/services/ripple-mcp/src:${PWD}/core/src:${PWD}/axon/src${PYTHONPATH:+:${PYTHONPATH}}"
export NEO4J_USER NEO4J_PASSWORD NEO4J_DATABASE REPO_ID MINI_REPO_ID

PHASE3_TMP_DIR="$(mktemp -d -t ripple-phase3.XXXXXX)"
GATEWAY_UP_JSON="${PHASE3_TMP_DIR}/gateway-up.json"
ASK_JSON="${PHASE3_TMP_DIR}/ask.json"
CALLERS_TXT="${PHASE3_TMP_DIR}/callers.txt"
INGEST_JSON="${PHASE3_TMP_DIR}/ingest.json"
FOLLOW_JSON="${PHASE3_TMP_DIR}/follow.json"
ENGINE_LOG="${PWD}/infra/rocketride/engine.phase3.log"
MCP_LOG="${PWD}/.ripple_mcp.phase3.log"

cleanup() {
  local status=$?
  "${PYTHON_BIN}" -m ripple_gateway down >/dev/null 2>&1 || true
  if [[ "${status}" -ne 0 ]]; then
    echo "Phase 3 smoke failed. Recent logs:" >&2
    if [[ -f "${ENGINE_LOG}" ]]; then
      echo "--- engine log ---" >&2
      tail -80 "${ENGINE_LOG}" >&2 || true
    fi
    if [[ -f "${MCP_LOG}" ]]; then
      echo "--- mcp log ---" >&2
      tail -80 "${MCP_LOG}" >&2 || true
    fi
  fi
  rm -rf "${PHASE3_TMP_DIR}"
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

wait_for_port() {
  local host="$1"
  local port="$2"
  local label="$3"
  for _ in {1..80}; do
    if "${PYTHON_BIN}" - "${host}" "${port}" <<'PY' >/dev/null 2>&1
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
with socket.create_connection((host, port), timeout=0.25):
    pass
PY
    then
      return 0
    fi
    sleep 0.25
  done
  echo "${label} did not open ${host}:${port}" >&2
  return 1
}

ensure_neo4j() {
  if ! docker inspect "${NEO4J_CONTAINER}" >/dev/null 2>&1; then
    echo "Neo4j container ${NEO4J_CONTAINER} does not exist" >&2
    exit 1
  fi
  if ! docker ps --format '{{.Names}}' | grep -qx "${NEO4J_CONTAINER}"; then
    docker start "${NEO4J_CONTAINER}" >/dev/null
  fi
  for _ in {1..80}; do
    if cypher "RETURN 1 AS ok" >/dev/null 2>&1; then
      echo "Neo4j: up"
      return 0
    fi
    sleep 0.25
  done
  echo "Neo4j did not become queryable" >&2
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
    echo "${engine_pid}" > .engine.phase3.pid
    disown "${engine_pid}" 2>/dev/null || true
  )

  for _ in {1..120}; do
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

ensure_mcp() {
  if "${PYTHON_BIN}" - "${MCP_HOST}" "${MCP_PORT}" <<'PY' >/dev/null 2>&1
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
with socket.create_connection((host, port), timeout=0.25):
    pass
PY
  then
    echo "RIPPLE MCP: up"
    return 0
  fi

  echo "Starting RIPPLE MCP"
  if command -v launchctl >/dev/null 2>&1; then
    launchctl remove "${MCP_LAUNCHD_LABEL}" >/dev/null 2>&1 || true
    launchctl submit \
      -l "${MCP_LAUNCHD_LABEL}" \
      -o "${MCP_LOG}" \
      -e "${MCP_LOG}" \
      -- /bin/bash -lc "cd '${PWD}' && exec scripts/run_mcp.sh"
    echo "launchctl:${MCP_LAUNCHD_LABEL}" > .ripple_mcp.phase3.pid
  else
    nohup scripts/run_mcp.sh >"${MCP_LOG}" 2>&1 < /dev/null &
    mcp_pid=$!
    echo "${mcp_pid}" > .ripple_mcp.phase3.pid
    disown "${mcp_pid}" 2>/dev/null || true
  fi
  wait_for_port "${MCP_HOST}" "${MCP_PORT}" "RIPPLE MCP"
  echo "RIPPLE MCP: started"
}

ensure_axon_self() {
  local repo_count
  repo_count="$(count_query "MATCH (repo:Repo {id: '${REPO_ID}'}) RETURN count(repo) AS count")"
  if [[ -n "${repo_count}" && "${repo_count}" -gt 0 ]]; then
    echo "Repo ${REPO_ID}: present"
    return 0
  fi
  echo "Repo ${REPO_ID}: missing, running Phase 1 ingest"
  scripts/smoke_phase1.sh
}

validate_initial_answer() {
  "${PYTHON_BIN}" - "${ASK_JSON}" "${CALLERS_TXT}" <<'PY'
import json
import re
import sys
from pathlib import Path

answer = json.loads(Path(sys.argv[1]).read_text())
impact = answer.get("impact")
cypher_used = answer.get("cypher_used")
assert isinstance(impact, list) and impact, answer
assert isinstance(cypher_used, list) and cypher_used, answer

callers = set()
for line in Path(sys.argv[2]).read_text().splitlines():
    line = line.strip().strip('"')
    if ".py:" in line:
        callers.add(line)

top_fqns = [item.get("fqn") for item in impact[: min(3, len(impact))] if isinstance(item, dict)]
top_fqns = [fqn for fqn in top_fqns if isinstance(fqn, str)]
assert top_fqns, answer
missing = [fqn for fqn in top_fqns if fqn not in callers]
assert not missing, {"missing": missing, "callers_sample": sorted(callers)[:10], "answer": answer}
print("P2 ask:", {"impact": len(impact), "top_fqns": top_fqns, "cypher_used": len(cypher_used)})
PY
}

validate_followup_answer() {
  "${PYTHON_BIN}" - "${FOLLOW_JSON}" <<'PY'
import json
import re
import sys
from pathlib import Path

answer = json.loads(Path(sys.argv[1]).read_text())
cypher_used = answer.get("cypher_used")
assert isinstance(cypher_used, list) and cypher_used, answer
assert re.search(r"\d+", json.dumps(answer)), answer
print("P2 follow-up:", {"cypher_used": len(cypher_used), "numeric_answer": True})
PY
}

ensure_neo4j
ensure_engine
ensure_axon_self
ensure_mcp

"${PYTHON_BIN}" -m ripple_gateway up >"${GATEWAY_UP_JSON}"
"${PYTHON_BIN}" - "${GATEWAY_UP_JSON}" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text())
assert payload.get("p1", {}).get("token"), payload
assert payload.get("p2", {}).get("token"), payload
print("Gateway up:", {"p1": "ready", "p2": "ready"})
PY

"${PYTHON_BIN}" -m ripple_gateway ask \
  --repo-id "${REPO_ID}" \
  --question "What breaks if I change run_pipeline?" \
  --timeout 180 >"${ASK_JSON}"

cypher "
MATCH (target:Function {repo_id: '${REPO_ID}'})
WHERE target.name = 'run_pipeline' OR target.fqn ENDS WITH ':run_pipeline'
MATCH (caller:Function {repo_id: '${REPO_ID}'})-[:CALLS*1..4]->(target)
WHERE coalesce(caller.is_test, false) = false
RETURN DISTINCT caller.fqn AS fqn
" >"${CALLERS_TXT}"
validate_initial_answer

"${PYTHON_BIN}" -m ripple_gateway ingest \
  --repo-url "${PWD}/core/tests/fixtures/miniproj" \
  --repo-id "${MINI_REPO_ID}" \
  --timeout 180 >"${INGEST_JSON}"

mini_functions="$(count_query "MATCH (f:Function {repo_id: '${MINI_REPO_ID}'}) RETURN count(f) AS count")"
echo "P1 ingest: ${MINI_REPO_ID} functions=${mini_functions}"
if [[ -z "${mini_functions}" || "${mini_functions}" -le 0 ]]; then
  echo "Expected Function nodes for ${MINI_REPO_ID}" >&2
  exit 1
fi

"${PYTHON_BIN}" -m ripple_gateway ask \
  --repo-id "${REPO_ID}" \
  --question "How many uncovered functions with blast_score > 0.5 does axon-self have? Return the number and the Cypher." \
  --timeout 180 >"${FOLLOW_JSON}"
validate_followup_answer

"${PYTHON_BIN}" -m ripple_gateway down >/dev/null
echo "Gateway down: terminated"
echo "Phase 3 smoke passed"
