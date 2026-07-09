# Phase 5 Spec — Cognee Memory Layer

Context: `RIPPLE_system_design.md` §9 + §5.5. The memory layer that makes RIPPLE smarter
about a repo across sessions: agent writes decisions/fragility notes after verified fixes,
recalls them before planning, and a nightly `improve` job consolidates.

Infrastructure already present: Docker image `cognee/cognee:main` pulled; a SECOND Neo4j
container `ripple-neo4j-memory` runs at bolt://localhost:7688 (neo4j/memorypass, HTTP 7475)
— this is the stand-in for the design's "same Aura server, `memory` database" (community
edition has no multi-db; §5.5 explicitly blesses fallbacks). LLM key: GEMINI (see `.env`,
ROCKETRIDE_GEMINI_KEY) — cognee needs an LLM + embeddings provider; check cognee docs/env
(LLM_PROVIDER=gemini or custom endpoint; if gemini isn't supported cleanly, use
LLM_PROVIDER=custom with Gemini's OpenAI-compatible endpoint
https://generativelanguage.googleapis.com/v1beta/openai/ and model gemini-2.5-flash;
embeddings likewise or a local sentence-transformers fallback if cognee supports it).

## Deliverables

### 1. `infra/cognee/` — deployment

VERIFIED WORKING (2026-07-09, container `ripple-cognee-probe` — tear it down and replace
with the compose): image `cognee/cognee:main` with env `GRAPH_DATABASE_PROVIDER=neo4j`,
`GRAPH_DATABASE_URL=bolt://host.docker.internal:7688`, `GRAPH_DATABASE_USERNAME=neo4j`,
`GRAPH_DATABASE_PASSWORD=memorypass`, `LLM_PROVIDER=gemini`,
`LLM_MODEL=gemini/gemini-2.5-flash`, `LLM_API_KEY=<.env ROCKETRIDE_GEMINI_KEY>`,
`EMBEDDING_PROVIDER=gemini`, `EMBEDDING_MODEL=gemini/gemini-embedding-001`,
`EMBEDDING_API_KEY=<same>` → `/health` returns `{"status":"ready"}` (version 1.2.2-local).
(Health check passed; add/cognify/search still need live verification — do that in the
smoke test, and watch container logs for Neo4j auth/connection errors on first cognify.)

- `docker-compose.yml` + `.env.template` + `run.sh`: container name `ripple-cognee`,
  host port **127.0.0.1:8890** (8000 is occupied by an unrelated stack on this machine).
  SECURITY: every port mapping in this project MUST bind 127.0.0.1 explicitly — this
  machine has been observed receiving unsolicited external traffic on 0.0.0.0-bound ports.
  Volume for `/app/cognee/.cognee_system` (metadata + embedded dbs).
  If the Neo4j graph backend proves broken during cognify after honest debugging (capture
  logs), fall back to the embedded KuzuDB default per §5.5; note whichever worked in README.
- Health gate: wait for `GET /health` == 200.

### 2. `core/src/ripple/memory/client.py` — thin Cognee REST client

Async httpx client with the four verbs, dataset-per-repo isolation (§9):
- `remember(repo_id, text, meta: dict|None)` → cognee add + cognify into dataset repo_id
  (follow the REST API of the image: typically POST /api/v1/add then /api/v1/cognify;
  discover the exact routes from the running container's OpenAPI (`/openapi.json`) and
  encode them here).
- `recall(repo_id, query) -> list[{text, score?}]` → search endpoint on the dataset.
- `forget(repo_id)` → dataset delete (used by tests + future retention policies).
- `improve(repo_id)` → memify/optimize endpoint if the image exposes one; if not, document
  and implement as no-op returning {supported: false} (the nightly job then just re-cognifies).
Auth: the image's default credentials (check container docs/env — often default user; set
explicit creds in compose env and use them).

### 3. ripple-mcp: memory tools for the agent

- `memory_recall(repo_id, query)` — the agent calls this BEFORE planning (P2 instructions
  update below).
- `memory_remember(repo_id, note)` — the agent calls AFTER a verified fix (decision +
  fragility observations, §9 write-path format).
Also mirror rows: after each remember, insert into a local `memory_notes` mirror
(`chassis/` sqlite for now — file `chassis/ripple_local.db`, table
memory_notes(id, repo_id, summary, cognee_ref, created_at)) so the UI timeline (§7.1)
has a fast read path. Factor the sqlite access into `core/src/ripple/chassis_db.py`
(it will grow into the local Butterbase understudy in Phase 6).

### 4. P2 pipeline instruction update

- ask-mode: first tool wave includes `memory_recall(repo_id, question)`; if notes return,
  weave them into the answer under `memory_notes` (verbatim quotes, cite "from repo memory").
- fix-mode: after verification passes (and PR step), call `memory_remember` with a note:
  decision made, blast radius size, uncovered count, and any fragility observed (e.g. "N of
  M verify attempts failed on <file>'s tests first"). Include what was remembered in the
  final JSON `memory_notes`.

### 5. `services/gateway`: `nightly_memify` command

`python -m ripple_gateway memify` — iterates repos in Neo4j, calls improve (or re-cognify)
per dataset; log results. (Butterbase cron takes this over in Phase 6; keep the command
runnable standalone.)

### 6. Live smoke — `scripts/smoke_phase5.sh`

1. Bring up cognee (compose), health-gate.
2. Client-level: remember a distinctive fact into dataset `smoketest` ("Decision: token
   errors return 401 with WWW-Authenticate; security/oauth2.py is fragile"), then recall
   with "what did we decide about token errors?" → assert the recall result mentions
   401/WWW-Authenticate. forget(`smoketest`) afterward; assert recall is then empty/miss.
3. Agent-level (the §9 product moment, cross-session memory): POST a fix-mode request for
   miniproj through P2 (any small verified change) → assert memory_remember happened
   (mirror row exists in sqlite + final JSON memory_notes non-null). Then RESTART the P2
   pipeline task (fresh agent session, gateway down/up) and ask ask-mode: "what did we
   decide about <that change> and what should I be careful about?" → assert the answer's
   memory_notes cite the stored decision.
4. Exit nonzero on any failure; agent assertions structural, 180s timeouts.

## Quality bar
Typed, logged; no secrets committed (.env.template only, real values read from repo-root
`.env`). Do not modify axon/ or engine files. No git commits.
