#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$ROOT/infra/cognee/docker-compose.yml"

set -a
source "$ROOT/.env"
set +a

wait_ready() {
  for _ in $(seq 1 90); do
    if curl -fsS http://127.0.0.1:8890/health >/dev/null; then
      echo "Cognee is ready at http://127.0.0.1:8890"
      return 0
    fi
    sleep 2
  done
  echo "Cognee failed its health gate" >&2
  docker compose -f "$COMPOSE_FILE" logs --tail=200 cognee >&2
  return 1
}

case "${1:-up}" in
  up)
    export COGNEE_GRAPH_PROVIDER=kuzu
    export COGNEE_ACCESS_CONTROL=true
    docker compose -f "$COMPOSE_FILE" up -d cognee
    wait_ready
    ;;
  neo4j-probe)
    export COGNEE_GRAPH_PROVIDER=neo4j
    export COGNEE_ACCESS_CONTROL=false
    docker compose -f "$COMPOSE_FILE" --profile neo4j up -d neo4j-memory cognee
    wait_ready
    ;;
  down)
    docker compose -f "$COMPOSE_FILE" --profile neo4j down
    ;;
  logs)
    docker compose -f "$COMPOSE_FILE" logs --tail=200 cognee
    ;;
  *)
    echo "usage: $0 {up|neo4j-probe|down|logs}" >&2
    exit 2
    ;;
esac
