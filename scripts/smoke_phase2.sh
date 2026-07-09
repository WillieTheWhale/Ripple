#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
REPO_ID="${REPO_ID:-axon-self}"
NEO4J_CONTAINER="${NEO4J_CONTAINER:-ripple-neo4j}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
NEO4J_PASSWORD="${NEO4J_PASSWORD:-ripplepass}"
NEO4J_DATABASE="${NEO4J_DATABASE:-neo4j}"
API_PORT="${RIPPLE_API_PORT:-8787}"
API_URL="http://127.0.0.1:${API_PORT}"
API_LOG="$(mktemp -t ripple-api-smoke.XXXXXX.log)"
GRAPH_JSON_FILE="$(mktemp -t ripple-graph-smoke.XXXXXX.json)"

export REPO_ID

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

repo_count="$(count_query "MATCH (repo:Repo {id: '${REPO_ID}'}) RETURN count(repo) AS count")"
if [[ -z "${repo_count}" || "${repo_count}" -le 0 ]]; then
  echo "Repo ${REPO_ID} missing; running Phase 1 smoke ingest first"
  scripts/smoke_phase1.sh
fi

cleanup() {
  local status=$?
  if [[ -n "${api_pid:-}" ]]; then
    kill "${api_pid}" 2>/dev/null || true
    wait "${api_pid}" 2>/dev/null || true
  fi
  if [[ "${status}" -ne 0 ]]; then
    echo "--- API log ---" >&2
    cat "${API_LOG}" >&2 || true
  fi
  rm -f "${API_LOG}"
  rm -f "${GRAPH_JSON_FILE}"
  exit "${status}"
}
trap cleanup EXIT

export PYTHONPATH="${PWD}/services/api/src:${PWD}/core/src${PYTHONPATH:+:${PYTHONPATH}}"
"${PYTHON_BIN}" -m ripple_api >"${API_LOG}" 2>&1 &
api_pid=$!

echo "Started API pid=${api_pid} on ${API_URL}"

for _ in {1..40}; do
  if curl -fsS "${API_URL}/health" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "${api_pid}" 2>/dev/null; then
    echo "API process exited before /health became available" >&2
    exit 1
  fi
  sleep 0.25
done

health_json="$(curl -fsS "${API_URL}/health")"
"${PYTHON_BIN}" - "${health_json}" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
assert payload == {"status": "ok", "neo4j": "up"}, payload
print("Health:", payload)
PY

repos_json="$(curl -fsS "${API_URL}/repos")"
"${PYTHON_BIN}" - "${repos_json}" "${REPO_ID}" <<'PY'
import json
import sys

repos = json.loads(sys.argv[1])
repo_id = sys.argv[2]
match = next((repo for repo in repos if repo.get("repo_id") == repo_id), None)
assert match is not None, repos
stats = match.get("stats", {})
assert stats.get("functions", 0) > 500, stats
assert stats.get("calls", 0) > 1000, stats
print("Repos:", {"count": len(repos), "repo": repo_id, "functions": stats["functions"]})
PY

run_pipeline_resolve_json="$(curl -fsS -G "${API_URL}/repos/${REPO_ID}/resolve" --data-urlencode "q=run_pipeline")"
"${PYTHON_BIN}" - "${run_pipeline_resolve_json}" <<'PY'
import json
import sys

candidates = json.loads(sys.argv[1])
assert isinstance(candidates, list), candidates
print("Resolve run_pipeline:", candidates[:3])
PY

top_target_json="$("${PYTHON_BIN}" <<'PY'
import json
import os
import sys

from neo4j import GraphDatabase

driver = GraphDatabase.driver(
    os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
    auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "ripplepass")),
)
try:
    with driver.session(database=os.environ.get("NEO4J_DATABASE", "neo4j")) as session:
        record = session.run(
            """
            MATCH (f:Function {repo_id: $repo_id})
            WHERE coalesce(f.is_test, false) = false
              AND coalesce(f.blast_score, 0.0) > 0
              AND EXISTS {
                MATCH (caller:Function {repo_id: $repo_id})-[:CALLS]->(f)
                WHERE coalesce(caller.is_test, false) = false
              }
            RETURN f.fqn AS fqn, f.name AS name
            ORDER BY f.blast_score DESC, f.fqn ASC
            LIMIT 1
            """,
            repo_id=os.environ["REPO_ID"],
        ).single()
finally:
    driver.close()

if record is None:
    print("No production blast target found", file=sys.stderr)
    raise SystemExit(1)
print(json.dumps({"fqn": record["fqn"], "name": record["name"]}))
PY
)"

top_name="$("${PYTHON_BIN}" - "${top_target_json}" <<'PY'
import json
import sys

print(json.loads(sys.argv[1])["name"])
PY
)"
top_fqn="$("${PYTHON_BIN}" - "${top_target_json}" <<'PY'
import json
import sys

print(json.loads(sys.argv[1])["fqn"])
PY
)"

top_resolve_json="$(curl -fsS -G "${API_URL}/repos/${REPO_ID}/resolve" --data-urlencode "q=${top_name}")"
fqn="$("${PYTHON_BIN}" - "${top_resolve_json}" "${top_fqn}" <<'PY'
import json
import sys

candidates = json.loads(sys.argv[1])
target = sys.argv[2]
assert candidates, candidates
print(target if target in candidates else candidates[0])
PY
)"
echo "Selected blast target: ${fqn}"

blast_json="$(curl -fsS -G "${API_URL}/repos/${REPO_ID}/blast" \
  --data-urlencode "fqn=${fqn}" \
  --data-urlencode "max_hops=4" \
  --data-urlencode "limit=50")"
"${PYTHON_BIN}" - "${blast_json}" "${REPO_ID}" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
repo_id = sys.argv[2]
impacted = payload.get("impacted", [])
assert payload.get("target_fqn"), payload
assert payload.get("cypher"), payload
assert payload.get("params", {}).get("repo_id") == repo_id, payload.get("params")
assert len(impacted) == payload.get("total") and impacted, payload
risks = [node["risk"] for node in impacted]
assert risks == sorted(risks, reverse=True), risks
assert payload.get("uncovered_count", -1) >= 0, payload
print(
    "Blast:",
    {
        "target": payload["target_fqn"],
        "total": payload["total"],
        "top_risk": risks[0],
        "cypher_len": len(payload["cypher"]),
    },
)
PY

animation_json="$(curl -fsS -G "${API_URL}/repos/${REPO_ID}/blast/animation" \
  --data-urlencode "fqn=${fqn}")"
"${PYTHON_BIN}" - "${animation_json}" <<'PY'
import json
import sys

edges = json.loads(sys.argv[1])
assert isinstance(edges, list) and edges, edges
assert all(edge["hop"] >= 1 for edge in edges), edges[:5]
print("Animation:", {"edges": len(edges), "max_hop": max(edge["hop"] for edge in edges)})
PY

curl -fsS "${API_URL}/repos/${REPO_ID}/graph?max_nodes=2000" -o "${GRAPH_JSON_FILE}"
"${PYTHON_BIN}" - "${GRAPH_JSON_FILE}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
nodes = payload.get("nodes", [])
edges = payload.get("edges", [])
assert 0 < len(nodes) <= 2000, len(nodes)
assert 0 < len(edges) <= 8000, len(edges)
assert all({"uid", "fqn", "name", "label", "file_path"} <= set(node) for node in nodes[:20])
assert all({"source_uid", "target_uid", "type"} <= set(edge) for edge in edges[:20])
print("Graph:", {"nodes": len(nodes), "edges": len(edges)})
PY

echo "Phase 2 smoke passed"
