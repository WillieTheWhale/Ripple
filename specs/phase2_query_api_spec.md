# Phase 2 Spec — Blast-Radius Query + Read API (`core/` package + `services/api`)

Context: RIPPLE blast-radius copilot. Read `RIPPLE_system_design.md` §5.2 and §10. Phase 1
(`core/src/ripple/ingest/...`) is complete: Neo4j at bolt://localhost:7687 (neo4j/ripplepass,
db `neo4j`) holds the code knowledge graph per §5.1 with `blast_score`, `community`, `uid`
(`{repo_id}#{fqn}`), `repo_id` on every node. Reuse Phase 1's writer config pattern
(`ripple/ingest/writer.py` env-based config) — factor a shared `ripple/db.py` if helpful.

## Deliverables

### 1. `core/src/ripple/query/blast.py`

- `blast_radius(session_or_driver, repo_id: str, fqn: str, max_hops: int = 4, limit: int = 50)
   -> BlastResult`
- The §5.2 Cypher, parameterized by repo_id, adapted to the real schema. Requirements:
  - Reverse reachability over CALLS: dependents that (transitively, 1..max_hops) call the target.
  - `hops` = shortest path length; `centrality` = dependent.blast_score;
    `tests` = count of DISTINCT Test nodes with a COVERS edge to the dependent;
    `risk` = round(blast_score * (1.0/hops) * (tests == 0 ? 1.5 : 1.0), 3).
  - ORDER BY risk DESC LIMIT $limit.
  - Return also: the exact Cypher text used + parameters (transparency: the UI shows it verbatim).
- `BlastResult` dataclass: `target_fqn`, `impacted: list[ImpactedNode(fqn, name, file_path,
  hops, centrality, tests, risk, community)]`, `cypher: str`, `params: dict`,
  `uncovered_count: int`, `total: int`.
- `resolve_fqn(driver, repo_id, query: str) -> list[str]` — helper: exact fqn match first,
  then case-insensitive CONTAINS on Function.name, return up to 10 candidate fqns.

### 2. `core/src/ripple/query/graphview.py`

Read queries backing the frontend canvas:
- `graph_snapshot(driver, repo_id, max_nodes=2000) -> {nodes: [...], edges: [...]}`.
  Nodes: Function/Class/File selected by top blast_score first (Functions), with
  fqn/uid, name, label, file_path, blast_score, community, is_test, covered (bool: any COVERS
  inbound). Edges: CALLS/IMPORTS/DEFINES/INHERITS between included nodes
  (by uid pairs, with type). Cap edges at 8000, prefer CALLS.
- `repo_stats(driver, repo_id) -> {functions, classes, files, tests, calls, covers,
  uncovered_functions, avg_blast_score, communities}`.
- `ripple_paths(driver, repo_id, fqn, max_hops=4) -> [{source_uid, target_uid, hop}]` —
  the hop-by-hop edge list of the blast radius (for the UI's BFS ring animation): every CALLS
  edge on some shortest path from a dependent to the target, annotated with the ring index
  (distance of the edge's source from target).

### 3. `services/api/` — thin FastAPI read service (this is NOT the agent; read-only)

- `services/api/pyproject.toml` (name `ripple-api`, depends on `ripple` core path dep + fastapi + uvicorn).
- Endpoints (all JSON, CORS enabled for localhost dev):
  - `GET /health` → {status, neo4j: up|down}
  - `GET /repos` → list of Repo nodes with repo_stats each
  - `GET /repos/{repo_id}/graph?max_nodes=` → graph_snapshot
  - `GET /repos/{repo_id}/blast?fqn=&max_hops=&limit=` → BlastResult (with cypher included)
  - `GET /repos/{repo_id}/blast/animation?fqn=` → ripple_paths
  - `GET /repos/{repo_id}/resolve?q=` → resolve_fqn candidates
- Run with `uvicorn` on port 8787 (`python -m ripple_api` or a `scripts/run_api.sh`).

### 4. Tests + live smoke

- Unit tests for risk arithmetic (pure function extracted so it's testable without Neo4j)
  and Cypher text generation. `pytest core/tests services/api/tests -q` green.
- `scripts/smoke_phase2.sh`:
  1. Assumes axon-self ingested (Phase 1 smoke) — re-run Phase 1 ingest if repo missing
     (check via cypher-shell).
  2. Start the API (background, wait for /health).
  3. `curl` each endpoint; assert with jq/python: `/repos` includes axon-self;
     `/blast?fqn=<a real fqn>` returns >0 impacted, risk sorted desc, cypher non-empty;
     pick the fqn dynamically: resolve `run_pipeline` (or the top blast_score function) via
     `/resolve` first. `/blast/animation` returns >0 ring edges. `/graph` returns nodes+edges
     with counts within caps.
  4. Kill the API. Exit nonzero on any failure.
- Run everything and show output. Fix code, don't weaken assertions.

## Out of scope
Agent/RocketRide (Phase 3), fixes/Daytona (4), Cognee (5), frontend (7). No git commits.
