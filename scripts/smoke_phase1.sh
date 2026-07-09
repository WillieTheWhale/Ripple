#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
REPO_ID="${REPO_ID:-axon-self}"
NEO4J_CONTAINER="${NEO4J_CONTAINER:-ripple-neo4j}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
NEO4J_PASSWORD="${NEO4J_PASSWORD:-ripplepass}"
NEO4J_DATABASE="${NEO4J_DATABASE:-neo4j}"

"${PYTHON_BIN}" -m ripple.ingest \
  --repo ./axon \
  --repo-id "${REPO_ID}" \
  --wipe \
  --emit-progress

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

assert_gt() {
  local label="$1"
  local value="$2"
  local minimum="$3"
  echo "${label}: ${value} (expected > ${minimum})"
  if [[ -z "${value}" || "${value}" -le "${minimum}" ]]; then
    echo "Assertion failed: ${label}=${value}, expected > ${minimum}" >&2
    exit 1
  fi
}

function_count="$(count_query "MATCH (f:Function {repo_id: '${REPO_ID}'}) RETURN count(f) AS count")"
calls_count="$(count_query "MATCH (:Function {repo_id: '${REPO_ID}'})-[r:CALLS]->(:Function {repo_id: '${REPO_ID}'}) RETURN count(r) AS count")"
file_count="$(count_query "MATCH (f:File {repo_id: '${REPO_ID}'}) RETURN count(f) AS count")"
covers_count="$(count_query "MATCH (:Test {repo_id: '${REPO_ID}'})-[r:COVERS]->(:Function {repo_id: '${REPO_ID}'}) RETURN count(r) AS count")"
blast_count="$(count_query "MATCH (f:Function {repo_id: '${REPO_ID}'}) WHERE f.blast_score > 0 RETURN count(f) AS count")"
touched_count="$(count_query "MATCH (:Commit {repo_id: '${REPO_ID}'})-[r:TOUCHED]->(:File {repo_id: '${REPO_ID}'}) RETURN count(r) AS count")"

assert_gt "Function count" "${function_count}" 500
assert_gt "CALLS count" "${calls_count}" 1000
assert_gt "File count" "${file_count}" 100
assert_gt "COVERS count" "${covers_count}" 100
assert_gt "Functions with blast_score > 0" "${blast_count}" 100

echo "Top 5 by blast_score:"
cypher "MATCH (f:Function {repo_id: '${REPO_ID}'}) WHERE f.blast_score > 0 RETURN f.fqn AS fqn, f.blast_score AS blast_score ORDER BY blast_score DESC LIMIT 5"

assert_gt "Commit TOUCHED count" "${touched_count}" 50

