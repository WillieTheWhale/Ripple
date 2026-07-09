# Phase 1 Spec — RIPPLE Ingestion Service (`core/` package)

You are implementing the ingestion layer of RIPPLE, a blast-radius copilot. Read
`RIPPLE_system_design.md` at the repo root (§5 especially) for full context. This spec is
authoritative where they differ.

## What exists already (do not modify axon except where stated)

- `axon/` — vendored clone of harshkedia177/axon, installed editable in `axon/.venv`.
  Key APIs you will reuse:
  - `axon.core.ingestion.pipeline.run_pipeline(repo_path: Path, storage=None, embeddings=False)
     -> (KnowledgeGraph, PipelineResult)` — runs the full AST→graph extraction in memory.
     Pass `storage=None, embeddings=False`.
  - `KnowledgeGraph` (`axon.core.graph.graph`): `iter_nodes()`, `iter_relationships()`,
    `get_nodes_by_label(label)`, `get_relationships_by_type(rel_type)`, `get_node(id)`.
  - Node model (`axon.core.graph.model`): `NodeLabel` (FILE, FOLDER, FUNCTION, CLASS, METHOD,
    INTERFACE, TYPE_ALIAS, ENUM, COMMUNITY, PROCESS), `RelType` (CONTAINS, DEFINES, CALLS,
    IMPORTS, EXTENDS, IMPLEMENTS, MEMBER_OF, USES_TYPE, COUPLED_WITH, ...).
    `GraphNode` fields: id, label, name, file_path, start_line, end_line, content, signature,
    language, class_name, is_dead, is_entry_point, is_exported, properties(dict).
  - `axon.core.ingestion.community.export_to_igraph(graph) -> (ig.Graph, {vertex_idx: node_id})`
    — call+heritage graph over FUNCTION/METHOD/CLASS nodes, directed, weighted.
- Local Neo4j 5 in Docker: `bolt://localhost:7687`, user `neo4j`, password `ripplepass`,
  database `neo4j` (community edition, single DB). GDS plugin is installed but DO NOT depend
  on it — compute analytics in Python with igraph (§5.4 rung 2, the portable path that also
  works on Aura free tier).

## Deliverable

A Python package at `core/` (package name `ripple`, src layout: `core/pyproject.toml`,
`core/src/ripple/`). Python >= 3.12. Dependencies: `neo4j>=5`, `python-igraph`, `leidenalg`,
`GitPython` (or subprocess git — your choice), plus axon as a path dependency is NOT needed
at build time; at runtime we install both editable into one venv (see Verification).

### Modules

1. `ripple/ingest/extract.py` — wraps axon:
   - `extract(repo_path: Path) -> KnowledgeGraph` calling `run_pipeline(..., embeddings=False)`.

2. `ripple/ingest/mapping.py` — map axon graph → RIPPLE schema (§5.1 of the design doc):
   Nodes (every node gets `repo_id` property):
   - `(:Repo {id, name, default_branch, ingested_at})` — one per ingest run.
   - `(:File {path, loc, lang, repo_id})` from NodeLabel.FILE (loc = end_line or content line count).
   - `(:Function {fqn, name, file_path, start_line, end_line, loc, is_test, doc, repo_id})`
     from FUNCTION and METHOD. `fqn` = `file_path:qualified_name` where qualified_name is
     `ClassName.method_name` for methods (class_name field) else the function name.
     `is_test` = file basename matches `test_*.py`/`*_test.py` OR `/tests/` in path OR name
     starts with `test_`. `doc` = first line of docstring if extractable from `content`, else "".
   - `(:Class {fqn, name, file_path, repo_id})` from CLASS (also INTERFACE/ENUM → Class).
   - Functions with `is_test=true` ALSO get the `:Test` label (multi-label `:Function:Test`).
   - `(:Commit {sha, author, ts, message, repo_id})` — last N commits (default 200) via git log.
   Relationships:
   - `(:Repo)-[:CONTAINS]->(:File)`
   - `(:File)-[:DEFINES]->(:Function|:Class)`
   - `(:Function)-[:CALLS {count}]->(:Function)` from CALLS (count from edge properties if
     present, else 1). CALLS edges whose source or target is a Class (constructor calls) may be
     dropped or kept Function-to-Function only — document the choice.
   - `(:File)-[:IMPORTS]->(:File)` from IMPORTS (only file→file edges).
   - `(:Class)-[:INHERITS]->(:Class)` from EXTENDS/IMPLEMENTS.
   - `(:Test)-[:COVERS]->(:Function)` — see covers.py.
   - `(:Commit)-[:TOUCHED]->(:File)` from git log --name-only paths that match File nodes.

3. `ripple/ingest/covers.py` — COVERS edges, two strategies:
   - `static` (default): for each Test function, follow outgoing CALLS edges transitively up to
     depth 3 in the axon graph; every reached non-test Function gets a COVERS edge with
     `{depth}` property.
   - `dynamic`: stub for Phase 4 — implement the interface + raise NotImplementedError with a
     clear message (will run `coverage run -m pytest` with dynamic_context inside a sandbox).

4. `ripple/ingest/risk.py` — analytics computed in Python at ingest (§5.3, §5.4):
   - Build reversed CALLS igraph over Function nodes (reuse/adapt `export_to_igraph`, or build
     directly from mapped Function/CALLS data — simpler and decouples from axon labels).
   - `blast_score` = PageRank (damping 0.85) on the REVERSED call graph, min-max normalized to
     [0,1], rounded to 4dp. Nodes absent from call graph get 0.0.
   - `community` = Leiden (leidenalg, ModularityVertexPartition) community index on the
     undirected simplification. Absent nodes get -1.
   - Returned as `{fqn: score}` dicts, written onto Function nodes.

5. `ripple/ingest/writer.py` — Neo4j Bolt writer:
   - Config from env: `NEO4J_URI` (default `bolt://localhost:7687`), `NEO4J_USER` (`neo4j`),
     `NEO4J_PASSWORD` (`ripplepass`), `NEO4J_DATABASE` (`neo4j`).
   - On first run, ensure constraints/indexes (idempotent):
     `CREATE CONSTRAINT ... IF NOT EXISTS` on (Repo.id), (Function.fqn, Function.repo_id) node key
     or unique on composite via `(f.repo_id, f.fqn)` — use a uniqueness constraint on a
     synthetic `uid` property (`repo_id + '#' + fqn`) if composite constraints are enterprise-only
     (they are: node key = enterprise). So: every node gets `uid` (`{repo_id}#{fqn-or-path-or-sha}`)
     with a UNIQUE constraint per label, plus range indexes on Function.repo_id and Function.fqn.
   - `--wipe` mode: `MATCH (n {repo_id: $repo_id}) DETACH DELETE n` in batches (CALL {} IN
     TRANSACTIONS OF 10000 ROWS) before writing.
   - Writes: batched UNWIND MERGE (batch 1000) per label and per relationship type. MERGE on
     `uid`, SET all other properties. Relationships MERGE on endpoint uids + type.
   - Must log written counts per label/rel type.

6. `ripple/ingest/cli.py` + `__main__.py` — `python -m ripple.ingest`:
   ```
   --repo <path or git URL>   (URL → shallow clone to a workdir under .ripple_work/)
   --repo-id <string>          (required)
   --covers static|off         (default static)
   --commits <N>               (default 200)
   --wipe                      (delete existing repo_id data first)
   --emit-progress             (JSON lines on stdout: {"stage": str, "pct": float, "detail": str})
   ```
   Stages: clone, extract, map, covers, risk, write, done. Always emit a final JSON summary:
   `{"stage":"done","nodes":N,"edges":M,"functions":F,"tests":T,"covers":C,"duration_s":S}`.

### Quality bar

- Type hints everywhere, docstrings on public functions, no dead code, logging via `logging`.
- No global state; writer usable as a library (`IngestWriter(config).write(mapped_graph)`).
- Handle: repos with zero tests, functions with duplicate fqns (dedupe by keeping first, log
  warning), CALLS edges referencing symbols that didn't map (skip, count, log).

### Verification (you MUST run these and show output)

1. Create root venv: `uv venv .venv --python 3.12` then
   `uv pip install -e ./axon -e ./core --python .venv/bin/python` (axon deps included).
2. Unit tests in `core/tests/` with a fixture mini-repo (`core/tests/fixtures/miniproj/` —
   ~4 source files with functions/classes/calls + a `tests/test_mini.py` with 2 test functions
   that call into the source). Assert: mapping produces expected Function/Class/File counts,
   is_test flags, static COVERS edges exist, blast_score of the most-called function is the
   maximum, fqn format correct. Run with `.venv/bin/python -m pytest core/tests -q`.
   These tests must NOT require Neo4j (pure mapping/risk layers).
3. Live smoke: `scripts/smoke_phase1.sh` —
   a. `python -m ripple.ingest --repo ./axon --repo-id axon-self --wipe --emit-progress`
   b. Then run assertion queries via cypher-shell in docker
      (`docker exec ripple-neo4j cypher-shell -u neo4j -p ripplepass "..."`):
      - Function count > 500, CALLS count > 1000, File count > 100
      - `MATCH (t:Test)-[:COVERS]->(f:Function) RETURN count(*)` > 100
      - `MATCH (f:Function) WHERE f.blast_score > 0 RETURN count(f)` > 100
      - Top-5 by blast_score printed (sanity: should be widely-called utilities)
      - `MATCH (c:Commit)-[:TOUCHED]->(:File) RETURN count(*)` > 50
   The script must exit nonzero if any assertion fails (parse counts with awk/grep).
4. Run the smoke script and show its output. Fix anything that fails. Do not weaken
   assertions to make them pass; fix the code.

### Out of scope for this phase

Blast-radius query API (Phase 2), RocketRide pipelines, Daytona, Cognee, frontend.
Do not touch `axon/` source except reading it. Do not create git commits.
