# Phase 3 Spec — RocketRide Pipelines P1 (ingest) + P2 (ask/fix agent)

Context: RIPPLE (`RIPPLE_system_design.md` §6). Phases 1–2 done: `core/` package ingests
repos into Neo4j and serves blast-radius queries; `services/api` is the read API.
This phase builds the two RocketRide pipelines and proves them live against the local
self-hosted engine (the design's §12 understudy; the same JSON deploys to Cloud later).

## Hard-won integration facts (do not rediscover these — they are verified)

- Engine: RocketRide v3.3.1 self-hosted, running at `ws://localhost:5565`
  (binary at `infra/rocketride/engine`, started with
  `ROCKETRIDE_APIKEY=ripple-local-dev-key ./engine ./ai/eaas.py --host=127.0.0.1`).
  If it's not running (`curl http://localhost:5565/version -H "Authorization: Bearer
  ripple-local-dev-key"`), start it that way from `infra/rocketride/` with its
  `.venv` activated.
- Auth: single shared secret. Client: `RocketRideClient(uri='ws://localhost:5565',
  auth='ripple-local-dev-key')` (param is `auth`, NOT `apikey`). Env fallbacks:
  ROCKETRIDE_URI / ROCKETRIDE_APIKEY (see repo-root `.env`).
- Start a pipeline: `await client.use(pipeline=<flat dict>, name='...')` where the dict has
  top-level `components`, `project_id` (any stable UUID), AND `source` (the id of the source
  component). Returns `{token: 'tk_...', publicToken: 'pk_...', ...}`.
- Trigger: `POST http://localhost:5565/webhook?token=<tk_...>` with header
  `Authorization: Bearer ripple-local-dev-key`. Body routed by content type; the `response*`
  node's captured lanes come back in the same HTTP response under
  `data.objects.body.<result key>` (e.g. `answers` for provider `response_answers` with
  `config.laneName: "answers"`).
- VERIFIED LIVE (a Gemini agent answered "391" to 17*23 through this exact path): POST with
  header `Content-Type: lane/questions` and body `{"questions": [{"text": "<the question —
  put the whole JSON task payload here as a string>"}]}` (Question schema:
  `infra/rocketride/rocketride/schema/question.py`; the agent reads `questions[].text`;
  a bare `{"text": ...}` does NOT reach the agent). `lane/<name>` content types force lane
  routing (`ai/modules/data/data_conn.py:_determine_lane`). The answer returns in the same
  HTTP response at `data.objects.body.answers[0]`.
- VERIFIED: llm_gemini works with profile `gemini-3-flash-preview` (also available:
  gemini-3-pro-preview, gemini-2.5-pro — see nodes/llm_gemini/services.json). Use
  gemini-3-pro-preview for the P2 agent LLM (planning quality) and gemini-3-flash-preview
  for the db_neo4j Cypher-generation LLM (fast, cheap). API key: fresh key in `.env`
  ROCKETRIDE_GEMINI_KEY (minted 2026-07-09 on the user's GCP project; the engine does NOT
  see your shell env — inline the key into the .pipe at gateway load time by reading .env
  and substituting ${ROCKETRIDE_GEMINI_KEY} etc. client-side before client.use(); commit
  pipes with the ${VAR} placeholders only).
- A working reference pipe (webhook→agent_rocketride(llm_gemini+memory_internal)→
  response_answers) that produced the live answer: /tmp/agent-test.pipe — copy its
  structure.
- .pipe JSON format (see working examples in `specs/examples/*.pipe`):
  - `components[]`: `{id, provider, name?, config{...}, input: [{lane, from}],
    control: [{classType: llm|memory|tool, from: <agentId>}]}` — note `control` lives on the
    *provider* node (llm/tool/memory) and points back at the agent via `from`.
  - `${ENV_VAR}` substitution works inside config values.
  - LLM node config is profile-based: `{"profile": "<name>", "<name>": {"apikey": "..."}}`.
- LLM: use provider `llm_gemini` with `${ROCKETRIDE_GEMINI_KEY}` (real key in repo-root
  `.env`; export env vars from `.env` before starting the engine/client so substitution
  resolves — check whether substitution happens client-side or engine-side by testing; if
  engine-side, the engine must be restarted with the vars exported). Inspect
  `infra/rocketride/nodes/llm_gemini/README.md` for its profile names/model field and pick a
  strong model (e.g. gemini-2.5-pro / gemini-3-pro if listed). Keep an `llm_anthropic`
  component JSON in the pipe as a comment or in README for the Cloud swap (§6.2 says
  Anthropic; we run Gemini locally because that's the key we have).
- Agent: provider `agent_rocketride` ("Wave"), config `{instructions: [...], max_waves: N,
  parameters: {}}`, requires `memory` control connection (`memory_internal`) and one `llm`.
  Full behavior documented in `infra/rocketride/nodes/agent_rocketride/README.md`.
- db_neo4j node: provider `db_neo4j`. Config fields (see its README): `uri`
  (`bolt://localhost:7687`), `auth_method: userpass`, `user: neo4j`,
  `password: ${NEO4J_PASSWORD or literal ripplepass}`, `database: neo4j`, `db_description`
  (write a rich one — §6.2), `max_attempts: 5`, `allow_execute: false`. Exposes
  `get_data/get_schema/get_cypher` tools to the agent. It ALSO needs an `llm` control
  connection of its own (it generates Cypher with an LLM) — wire the same or a second
  llm_gemini node to it. IMPORTANT: verify with the node source (`IInstance.py`) whether a
  tool-only node still needs the `questions` input lane — it should not.
- MCP tools: provider `tool_mcp_client` (see README for transport config:
  streamable-http endpoint + serverName + optional bearer).

## Deliverables

### 1. `services/ripple-mcp/` — RIPPLE's own MCP server (Python, FastMCP or the official
`mcp` package, streamable-http transport on port 8790)

CRITICAL CONSTRAINT: tool_mcp_client enforces a 20-second timeout per tools/call. Any
tool that can exceed ~15s MUST be job-shaped: return `{job_id, status:"running"}`
immediately from a background thread/task, with a `job_status(job_id) -> {status:
running|done|failed, result?, error?, progress?}` tool the agent polls. Fast queries
(blast_radius, resolve, stats) stay synchronous.

Tools (thin wrappers over `core/` — import ripple package directly):
- `ingest_repo(repo_url_or_path: str, repo_id: str, wipe: bool = true) -> {job_id}` —
  job-shaped (see above). Runs the Phase 1 ingest in a worker thread (this is where Bolt
  WRITES happen; the agent never writes — §4b). Progress stages surfaced via job_status.
- `job_status(job_id)` — poll tool for all job-shaped tools.
- `blast_radius(repo_id: str, fqn: str, max_hops: int = 4) -> BlastResult as dict` — Phase 2
  canned query (the "canned Cypher behind a UI toggle" fallback of §12, and the reliable
  path the agent can use when db_neo4j NL→Cypher is flaky; the returned dict includes the
  exact Cypher text for transparency).
- `resolve_symbol(repo_id: str, query: str) -> [fqns]`.
- `repo_stats(repo_id: str) -> dict`.
Runnable: `scripts/run_mcp.sh` (activates root `.venv`, `python -m ripple_mcp`).

### 2. `pipelines/ripple_ingest.pipe` — P1

webhook source → agent_rocketride (an "ingestion operator" with max_waves 4, instructions:
parse the incoming JSON `{repo_url, repo_id}`, call `ripple.ingest_repo`, then answer with
the JSON summary verbatim) with tools: tool_mcp_client → ripple-mcp. → response_answers.
(P1's compute deliberately lives in the MCP service, NOT in agent reasoning; the agent is
just the trigger/reporter. Graph writes stay out of the agent per the design.)

### 3. `pipelines/ripple_ask.pipe` — P2 (the brain)

- webhook source → agent_rocketride (max_waves 15) → response_answers.
- Agent instructions = the §6.2 agent policy, concretely: you are RIPPLE's blast-radius
  copilot for Python repos ingested into a Neo4j knowledge graph (schema: Repo/File/Function
  (:Test)/Class/Commit; CALLS/IMPORTS/DEFINES/INHERITS/COVERS/TOUCHED; Function.blast_score
  = reversed-CALLS PageRank; Function.community; uid = repo_id#fqn). Input is JSON
  {repo_id, mode: ask|fix, question, intent}. For ask-mode: prefer `ripple.blast_radius` for
  the core impact query (it returns the exact Cypher — include that Cypher in your answer),
  use `neo4j.get_data` for ad-hoc follow-up queries; ALWAYS answer with a single JSON object
  {impact: [...], summary: str, cypher_used: [...], memory_notes: str|null}. Never invent
  graph facts — every claim must come from a tool result. (fix-mode tools arrive in Phase 4;
  if mode=fix, reply that fix mode is not yet enabled.)
- Tools wired: tool_mcp_client → ripple-mcp (port 8790), db_neo4j (read-only, rich
  db_description describing the exact schema above including property names and the §5.2
  risk semantics), memory_internal, llm_gemini.
- Set `"source"` and `project_id` top-level fields in both .pipe files. Keep `ui` blocks
  minimal or omit them if the engine accepts pipes without them (test it).

### 4. `services/gateway/` — tiny Python runner (this is the local stand-in for the
Butterbase fn + keeps ops simple):

- `python -m ripple_gateway up` — loads `.env`, connects RocketRideClient, `use()`s both
  .pipe files (idempotent: `get_task_token(project_id, source)` first; reuse running task),
  writes `{p1: {token}, p2: {token}}` to `.ripple_tasks.json`.
- `python -m ripple_gateway ask --repo-id X --question "..."` — POSTs to the P2 webhook,
  prints the structured JSON answer. Same for `ingest --repo-url --repo-id` → P1.
- `python -m ripple_gateway down` — terminates both tasks.

### 5. Live smoke test — `scripts/smoke_phase3.sh` (MUST run green before you're done)

1. Preconditions: Neo4j container up; engine up (start if needed); axon-self repo ingested
   (from Phase 1; re-ingest if missing); ripple-mcp running (start in background).
2. `ripple_gateway up` → both pipelines start, tokens saved.
3. Ask through P2: question "What breaks if I change run_pipeline?" repo_id axon-self →
   assert: HTTP 200, answer parses as JSON, `impact` non-empty, `cypher_used` non-empty,
   the top impacted fqns are actual callers of run_pipeline (cross-check with a direct
   cypher-shell query in the script).
4. P1 ingest of a SECOND small repo: use a local path (e.g. `core/tests/fixtures/miniproj`)
   with repo_id `miniproj` → assert Neo4j has Function nodes for repo_id miniproj afterward.
5. Ask a follow-up question that forces the agent to use `neo4j.get_data` (e.g. "How many
   uncovered functions with blast_score > 0.5 does axon-self have? Return the number and the
   Cypher.") → assert numeric answer present.
6. Teardown: gateway down (leave engine + mcp running). Exit nonzero on any failed assertion.

Notes: the agent is nondeterministic — make assertions structural (JSON parses, fields
non-empty, fqns exist in graph) not exact-match. Give the agent up to 180s per question.
If ${VAR} substitution turns out not to work for some field, inline the literal value for
local dev and note it in the README. If webhook JSON routing to the questions lane fails,
try text/plain with the JSON as the text body — the agent can parse it either way; document
what worked.

## Quality bar
Python: typed, logged, no dead code. The .pipe files must be valid JSON loadable by
`client.use()`. Do not modify `axon/`, `core/src/ripple/ingest`, or `infra/rocketride/`
engine files. Do not create git commits.
