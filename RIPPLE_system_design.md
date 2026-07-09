# RIPPLE — System Design Document
### A blast-radius copilot that proves its fixes before you ship them
**Base engine:** `harshkedia177/axon` (open source, disclosed; see §11)
**Core infrastructure:** Butterbase · Neo4j · RocketRide Cloud
**Extended capability:** Daytona · Cognee

---

## 1. Thesis

Every engineer has shipped a change that *looked* isolated and broke something three modules away. RIPPLE ingests a repository into a Neo4j knowledge graph, shows the exact blast radius of a proposed change as a live graph animation, then has an agent draft the fix and **prove it passes the test suite in an isolated Daytona sandbox** — before a human touches anything. Across sessions, a Cognee memory layer learns which parts of the codebase are fragile and which past decisions constrain future ones, so the copilot gets smarter about *your* repo every day.

The design is oriented around a proven category: code-relationship graphs that visualize change impact, paired with agents that find, fix, and verify autonomously, are one of the strongest current patterns in developer-tooling AI products — teams building in this space have consistently earned praise for demos where the graph itself carries the story and the agent's work is independently verified rather than asserted. RIPPLE combines both halves into one product.

The novel architectural claim (what no vetted prior-art repo in the 50–999★ GitHub band does): **the agent's entire brain runs inside a managed RocketRide Cloud pipeline**, where Neo4j, Daytona, GitHub, and Butterbase are wired in as *agent tools* using RocketRide's own first-party nodes — the application layer is deliberately thin. And the blast-radius answer is not a static traversal: it is **risk-weighted** (graph centrality × test-coverage gap × Cognee-remembered fragility), and every proposed fix ships with a **sandbox-verified proof**, not just a plausible-looking diff.

---

## 2. Verified sponsor/vendor capability matrix (docs, not landing pages)

Every capability below was verified against the vendor's live documentation. Items marked ⚠ are real constraints the landing pages do not advertise, and this design engineers around them rather than assuming them away.

| Vendor | Verified capabilities we use | ⚠ Doc-revealed constraints that shaped this design |
|---|---|---|
| **Butterbase** (docs.butterbase.ai) | Per-app Postgres + declarative JSON schema + auto CRUD Data API; email/OAuth auth with JWT; RLS in one call; S3-style storage via presigned URLs; TS/JS serverless functions (HTTP + cron); static frontend deploy to a live `*.butterbase.dev` URL; OpenAI-compatible AI model gateway (Claude/GPT/Llama, BYOK or shared key); realtime WebSocket with RLS-aware filtering; **Stripe Connect monetization** with documented plan/subscribe/cancel endpoints; native RAG; MCP server exposing all of it as tools | ⚠ The MCP server is the fastest path to standing up schema/auth/RLS correctly — use it for scaffolding rather than hand-writing boilerplate. Feature usage (auth, storage, realtime, functions) should be genuine product needs, not decorative — each is wired below because the product actually needs it. |
| **Neo4j** | Property graph + Cypher; Aura free tier over `neo4j+s://` (TLS) with user/pass or bearer token; Graph Data Science algorithms (PageRank/centrality, community detection, shortest path) | ⚠ None beyond ops hygiene; Aura free tier is the deployment target (no guaranteed self-managed GDS plugin — see §5.4 fallback) |
| **RocketRide** (docs.rocketride.org) | 109 nodes / 20 types; `.pipe` portable JSON; Cloud = set `ROCKETRIDE_URI=https://api.rocketride.ai` + `ROCKETRIDE_AUTH`, same JSON as local; **Webhook source node** (HTTP in), **Response node** (answer out); native **Wave agent** (wave-planned parallel tool calls, keyed memory) plus LangChain/CrewAI/DeepAgent nodes; **first-party `tool_daytona` node** (run_code, run_command, upload_file, download_file on a shared sandbox); **first-party `tool_mcp_client` node documented as connecting to the Butterbase MCP server**; `tool_github` (files, PRs, issues, commit history) and `tool_git`; `db_neo4j` node; `preprocessor_code` (source-code parsing/chunking); per-node tracing (tokens, latency, call trees) | ⚠ **`db_neo4j` is read-only by design**: generated Cypher is restricted to MATCH/RETURN-class clauses, safety-filtered twice, EXPLAIN-validated with retry (max_attempts), 30s timeout. Writes exist only behind the opt-in `allow_execute: true` EXECUTE path (off by default, 25k-row cap). Design consequence: **graph writes never go through the agent** — ingestion writes via the Neo4j Bolt driver in a dedicated pipeline step; the agent's `neo4j.get_data / get_schema / get_cypher` tools stay read-only (defence in depth, and a genuine security posture worth keeping even post-launch). Schema is reflected at pipeline start via `db.schema.*` and injected into every Cypher-generation prompt. |
| **Daytona** (daytona.io/docs, PyPI `daytona`) | Python/TS SDKs: `daytona.create()`, `sandbox.git.clone()`, `sandbox.process.exec()` / `code_run()`, `sandbox.fs.upload/download/find`, LSP servers; **snapshots** = prebuilt sandbox templates for instant cold-start with deps preinstalled; declarative Dockerfile images; official LangChain sandbox backend; MCP server | ⚠ The original open-source repo was archived to private development in June 2026 — the **platform, dashboard, and SDKs are live and current** (PyPI releases ongoing); we integrate via the hosted platform + SDK, and additionally via RocketRide's `tool_daytona` node so the sandbox is an *agent tool*, not app code. Note the node's sandbox is "one shared ephemeral sandbox" per pipeline — fine for early scale; per-request sandboxes via the raw SDK are the scale-up path (§8, §12). |
| **Cognee** (github.com/topoteretes/cognee, docs.cognee.ai) | OSS (Apache-2.0) memory engine with four verbs: `remember / recall / forget / improve`; session memory + permanent graph; pluggable graph backends incl. **Neo4j** (`docker compose --profile neo4j up`; `GRAPH_DATABASE_PROVIDER` env selects backend); REST API server image (`cognee/cognee:main`), MCP server image; Python/TS/Rust clients; per-user dataset isolation | ⚠ We standardize on **Open Source Cognee**, self-hosted, rather than Cognee Cloud — this keeps the memory layer under our own operational control and avoids a third-party managed dependency for a core feature. |

The one integration the docs make easier than expected: RocketRide already ships nodes purpose-built for three of the other vendors (Neo4j, Daytona, Butterbase-via-MCP). Deep integration across the stack is therefore not glue code we have to invent — it is the documented, first-party wiring of the ecosystem itself, which is a strong reason to build on RocketRide Cloud as the orchestration layer rather than rolling a custom agent runtime.

---

## 3. Product definition

**One-liner:** *"See what breaks before you break it — and get a fix that's already proven."*

**Primary user journey:**
1. Sign in with GitHub (Butterbase OAuth). Pick a repo (initial support: a curated set of well-scoped Python repos, chosen for graphs that render well and test suites that run quickly — broader "bring your own repo" support is a fast-follow, see §12).
2. The repo's knowledge graph renders live (files → functions → calls → imports → tests), with a **risk heatmap**: node size = blast-radius score, color = fragility memory.
3. Ask: *"What breaks if I change `OAuth2PasswordBearer.__call__`?"* → the graph **ripples**: shortest paths from the target light up hop by hop; a ranked impact list appears with risk scores and the Cypher that produced it (transparency by design, not an afterthought).
4. Click **"Fix it for me"** with a change intent (e.g., "make token errors return 401 with a WWW-Authenticate header") → the RocketRide Wave agent plans; the user watches its tool calls stream: `neo4j.get_data` → patch drafted → `daytona.run_command("pytest …")` → tests run and pass → a unified diff + verification log lands in the UI, and `tool_github` opens a draft PR.
5. In a new session, ask "what did we decide about token errors last week?" → Cognee recalls the decision and the fragility note it stored.
6. Private repos and multi-repo support sit behind a **Pro plan ($9/mo via Stripe Checkout)** — real monetization from day one, not a placeholder.

**Deliberate initial scope boundaries (not permanent limitations — sequencing choices, see §12 for the roadmap):** Python-only ingestion first (axon's strength); a curated repo allowlist before general "bring your own repo" support; draft PRs rather than auto-merge; simple per-user access before fine-grained team RBAC.

---

## 4. System architecture

```
                                   ┌────────────────────────────────────────────┐
                                   │            BUTTERBASE (app_id)             │
                                   │  Postgres (RLS)  Auth(GitHub OAuth)  KV    │
   Browser ── React graph UI ────► │  Storage  Realtime  Functions  Billing     │
   (axon GraphCanvas, sigma.js)    │  AI Gateway   Frontend @ *.butterbase.dev  │
        ▲            │             └───────┬───────────────────▲────────────────┘
        │ realtime   │ REST/JWT            │ serverless fn      │ MCP tools
        │ updates    ▼                     │ "askRipple"        │ (Butterbase MCP)
        │      ┌─────────────┐             ▼                    │
        └──────┤  Butterbase │      ┌──────────────────────────────────────────┐
               │  Realtime   │      │        ROCKETRIDE CLOUD (managed)        │
               └─────────────┘      │  api.rocketride.ai · two .pipe pipelines │
                                    │                                          │
                                    │  P1 INGEST: webhook → preprocessor_code  │
                                    │     → python(axon parse) → Neo4j writes  │
                                    │     (Bolt driver, NOT the agent)         │
                                    │                                          │
                                    │  P2 ASK/FIX: webhook → Wave agent ──────┼──► Response
                                    │     tools: db_neo4j (get_data/schema)   │
                                    │            tool_daytona (run tests)  ───┼──► DAYTONA sandbox
                                    │            tool_github (draft PR)       │    (snapshot: repo+deps)
                                    │            tool_mcp_client (Butterbase) │
                                    │            HTTP tool → Cognee API   ────┼──► COGNEE (OSS, self-hosted
                                    │            memory_internal (scratch)    │    in a Daytona sandbox)
                                    └───────────────┬──────────────────────────┘
                                                    │ Bolt (neo4j+s://)
                                            ┌───────▼────────┐
                                            │  NEO4J AURA    │  code KG: database "ripple"
                                            │  (free tier)   │  cognee memory: database "memory"
                                            └────────────────┘
```

**Control flow for one "what breaks / fix it" request:** Browser → Butterbase serverless function `askRipple` (verifies JWT, checks plan entitlement in Postgres, rate-limits via KV) → POSTs to the P2 webhook on RocketRide Cloud with `{repo_id, question, intent, user_ctx}` → Wave agent executes (Cypher traversals, patch, sandbox verification, memory read/write) → Response node returns the structured result → the function persists the analysis row in Butterbase Postgres (which fires a **realtime** event the UI is subscribed to) and returns. The UI therefore animates *as the pipeline works*, not after.

Two design decisions worth calling out explicitly: (a) the app server is almost stateless — Butterbase owns identity/data/billing, RocketRide Cloud owns cognition, Neo4j owns structure, Daytona owns execution, Cognee owns memory; each layer has a distinct, non-overlapping responsibility, which keeps the system debuggable as it grows. (b) All writes to the graph go through the ingestion path with the official Bolt driver, while the *agent* can only read the graph through RocketRide's safety-filtered `db_neo4j` tools — an LLM with a read-only, EXPLAIN-validated view of production data is a security posture worth keeping permanently, not a shortcut.

---

## 5. Neo4j design

### 5.1 Code knowledge graph (database: `ripple`)
Lifted from axon's ingestion model and extended with risk properties:

```
(:Repo {id, name, default_branch, ingested_at})
(:File {path, loc, lang})
(:Function {fqn, name, file_path, start_line, end_line, loc,
            complexity, is_test, doc})
(:Class {fqn, name, file_path})
(:Test {fqn, name, file_path})
(:Commit {sha, author, ts, message})            // last-N history
Relationships:
(:Repo)-[:CONTAINS]->(:File)
(:File)-[:DEFINES]->(:Function|:Class)
(:Function)-[:CALLS {count}]->(:Function)
(:File)-[:IMPORTS]->(:File)
(:Class)-[:INHERITS]->(:Class)
(:Test)-[:COVERS]->(:Function)                  // from pytest --collect + coverage map
(:Commit)-[:TOUCHED]->(:File)
```

### 5.2 The blast-radius query (the core product mechanic)
Reverse-reachability with decay, joined against coverage:

```cypher
MATCH (target:Function {fqn: $fqn})
CALL {
  WITH target
  MATCH p = (dependent:Function)-[:CALLS*1..4]->(target)
  RETURN dependent, min(length(p)) AS hops
}
OPTIONAL MATCH (t:Test)-[:COVERS]->(dependent)
WITH dependent, hops, count(t) AS tests
RETURN dependent.fqn AS impacted,
       hops,
       dependent.blast_score AS centrality,
       tests,
       round(dependent.blast_score * (1.0/hops) *
             CASE WHEN tests = 0 THEN 1.5 ELSE 1.0 END, 3) AS risk
ORDER BY risk DESC LIMIT 50
```

The 1.5× multiplier on *uncovered* dependents is the product insight: the scariest ripple is the one no test will catch. The UI colors those nodes red.

### 5.3 Risk scores: precomputed at ingest
`blast_score` is PageRank over the reversed CALLS graph (a function many things transitively depend on scores high); `community` is Louvain/Leiden for the module-cluster coloring in the UI. Both are computed **at ingestion time** and written as node properties, so query-time traversals stay fast and the agent's read-only tools can use them.

### 5.4 GDS on Aura free tier — fallback ladder
Aura's free tier may not expose the full GDS library. Ladder, in order: (1) Aura GDS if available on the instance; (2) compute PageRank/Louvain **in the ingestion pipeline's Python step** with `igraph` — axon already ships igraph-based community detection (`export_to_igraph` in its ingestion module), so this is a code path we lift, not write — and write scores back as properties; (3) degree-centrality-only (pure Cypher). The product experience is identical under all three; only the underlying computation changes, and this should be decided early rather than discovered under pressure later.

### 5.5 Cognee's graph — same server, separate database
Cognee is configured with `GRAPH_DATABASE_PROVIDER=neo4j` pointed at the **same Aura server, different database (`memory`)**. One Neo4j instance visibly hosting both the world-model and the memory-model keeps operational surface area small. (Fallback if Aura free tier limits multi-database: prefix-label isolation in the single DB, or Cognee's embedded Kuzu default — the memory feature survives either way; only the "same server" simplification is lost.)

---

## 6. RocketRide Cloud design (the brain)

Two pipelines, both built visually in the VS Code extension, committed as `.pipe` JSON, and deployed to Cloud (`ROCKETRIDE_URI=https://api.rocketride.ai`, token auth), so the app always calls a managed, production endpoint rather than a local process.

### 6.1 P1 — `ripple_ingest.pipe`
```
[webhook source]  ← POST {repo_url, repo_id} from Butterbase fn "ingestRepo"
   → [tool_git / clone step]        shallow clone to pipeline workspace
   → [preprocessor_code]            parse & chunk source (RocketRide native)
   → [python step: axon extractor]  build nodes/edges from AST + call resolution
                                    run igraph PageRank + Louvain (see §5.4)
   → [python step: Bolt writer]     MERGE nodes/edges into Aura `ripple` DB
                                    (writes happen HERE, not via db_neo4j)
   → [HTTP step → Butterbase]       PATCH repos.status='ready' → fires realtime
   → [Response]
```
Progress events are POSTed to Butterbase at each stage so the UI shows a live ingestion progress bar.

### 6.2 P2 — `ripple_ask.pipe` (the agent)
```
[webhook source] ← POST {repo_id, mode: "ask"|"fix", question, intent, user_ctx}
   → [RocketRide Wave agent] ── llm: Anthropic node (Claude)
        tools wired on the canvas:
        • db_neo4j        (uri=neo4j+s://<aura>, database=ripple,
                           db_description="Python code knowledge graph: files,
                           functions, CALLS/IMPORTS/COVERS edges, blast_score…",
                           allow_execute=false)          ← read-only by design
        • tool_daytona    (run_command / run_code / upload_file)
        • tool_github     (create branch, commit patch, open draft PR)
        • tool_mcp_client (Butterbase MCP: persist analysis rows, read history)
        • HTTP tool       (Cognee REST: POST /recall before planning,
                           POST /remember after verification)   [whitelisted URL]
        • memory_internal (scratch keyed memory between planning waves)
   → [Response] structured JSON: {impact[], diff, verification{cmd, passed,
                                  log_tail}, pr_url, cypher_used[], memory_notes}
```

**Agent policy (system prompt, abridged):** *Plan in waves. For impact questions, call `neo4j.get_data` with the blast-radius intent; include the Cypher in your answer for transparency. For fixes: read the target and dependents via `neo4j.get_data`; draft a minimal patch; upload it to the Daytona sandbox; run the scoped test command; if red, iterate (max 3); only after green, open a draft PR and `remember` the decision + any fragility observed. Never claim verification you did not run.* The `db_neo4j` node's own EXPLAIN-retry loop (max_attempts=5) handles Cypher syntax robustness below the agent, and its double safety filter guarantees the agent cannot mutate the graph even if prompted maliciously — a property worth preserving as the system matures, not just a launch-time safeguard.

**Why Wave over LangChain/CrewAI nodes:** it is RocketRide's native agent, wave-planned parallel tool calls are visibly faster on the multi-tool loop, and its keyed-memory pattern keeps the context window small. The agent node is swappable for `agent_langchain` with identical tool wiring — a config edit in the canvas, not a rewrite — which is useful insurance if Wave's behavior ever needs to be A/B tested against alternatives.

**Observability:** RocketRide Cloud's per-node tracing (call trees, token spend, latency) should be treated as a first-class operational tool, not just a debugging convenience — it's the fastest way to answer "is this agent actually doing what it claims" at any point in the product's life.

---

## 7. Butterbase design (product chassis)

### 7.1 Schema (declarative JSON, applied via MCP)
```
users        (managed by Butterbase auth; GitHub OAuth enabled)
repos        (id, owner_user_id, gh_url, name, status, node_count, edge_count)
analyses     (id, repo_id, user_id, mode, question, impact_json, risk_top,
              cypher_json, created_at)
fixes        (id, analysis_id, diff_text, verified bool, test_cmd,
              log_tail, pr_url, daytona_ms)
memory_notes (id, repo_id, summary, cognee_ref, created_at)   // mirror for UI
```
RLS on every user-owned table (one call per table, per docs). `analyses` and `fixes` inserts drive the realtime channel the UI subscribes to.

### 7.2 Serverless functions
`ingestRepo` (validates plan limits → POST P1 webhook), `askRipple` (JWT verify → entitlement check → rate limit via KV → POST P2 webhook → persist result), `stripeSuccess` (post-checkout landing logic), `nightlyMemify` (**cron** → calls Cognee `improve` on each active repo's dataset — a self-improving memory layer running on its own schedule with no manual intervention).

### 7.3 The core triad + AI gateway
Auth = GitHub OAuth (a natural fit for a developer tool). Database = everything above. **Payment** = Stripe Connect per the documented endpoints: one plan (`POST /v1/{app_id}/billing/plans` → "Ripple Pro", 900¢/mo, features: private repos, unlimited repos, priority pipeline) and the documented `subscribe → Checkout → webhook → subscription` loop; `askRipple` reads `GET /billing/subscription` for gating. **AI gateway**: the frontend's lightweight "explain this impact in plain English" tooltip calls the Butterbase AI API directly (OpenAI-compatible) — a genuinely distinct, user-visible use of the gateway from the pipeline's own Anthropic node, so both AI paths earn their place rather than duplicating each other.

---

## 8. Daytona design (the proof engine)

**Snapshot strategy:** build one snapshot per supported repo (`ripple-fastapi`, …): base Python image + `git clone` + `pip install -e .[test]` + a warm `pytest --collect-only`. `daytona.create(CreateSandboxFromSnapshotParams(snapshot="ripple-fastapi"))` then yields a ready-to-test sandbox in seconds — cold dependency installs are the single biggest source of slow, flaky live verification, and snapshots are the documented cure.

**Verification loop (as agent tool calls via `tool_daytona`):**
1. `upload_file(patch.diff, "/work/patch.diff")` → `run_command("cd /work/repo && git apply /work/patch.diff")`
2. `run_command("pytest <scoped test paths from the COVERS edges> -q --maxfail=5 --timeout=60")` — the graph tells us *which* tests to run; running only the blast radius's tests instead of the whole suite is the direct payoff of the Neo4j × Daytona combination.
3. Parse pass/fail + tail the log; on red, feed the failure back to the agent (≤3 iterations); on green, `run_command("git diff")` → hand to `tool_github` for the draft PR.

**Scope note:** RocketRide's `tool_daytona` uses one shared ephemeral sandbox per pipeline — fine early on; per-request sandboxes via the raw SDK are the natural scale-up path once concurrent usage grows (§12). **Composition note:** Cognee's own repo ships `distributed/deploy/daytona_sandbox.py`; running the self-hosted Cognee API *inside a Daytona sandbox* keeps memory-layer infrastructure consistent with the rest of the execution stack. (Fallback for the Cognee host: any small VM or local tunnel works too — this is an operational convenience, not a hard dependency.)

---

## 9. Cognee design (the memory)

Open Source Cognee, `cognee/cognee:main` API container, `GRAPH_DATABASE_PROVIDER=neo4j` → Aura `memory` DB (§5.5), one **dataset per repo** for isolation (documented multi-tenancy pattern).

**Write path (agent, post-verification):** `remember("Decision: token errors now return 401+WWW-Authenticate. Blast radius was 14 fns, 3 uncovered. security/oauth2.py is fragile: 2 of 3 fix attempts initially failed its tests.", dataset=repo_id)`.
**Read path (agent, pre-planning):** `recall(question ⊕ target_fqn, dataset=repo_id)` — recalled fragility notes raise the risk weighting the agent reports and make it choose more conservative patches.
**Improve path:** the `nightlyMemify` cron (§7.2) calls `improve` — prune stale nodes, reweight, derive facts — giving the memory layer the full verb set (*remember, recall, forget, improve*) rather than a one-way write log.
**Product moment:** a fresh session asking "what did we decide about token errors, and what should I be careful about?" should get an answer that cites the prior session's decision and the fragility note — memory that's demonstrably there across sessions, not just claimed.

---

## 10. Frontend

Lift axon's React graph stack wholesale — `GraphCanvas.tsx` (~1.8k LOC, sigma.js/graphology with ForceAtlas2 layout) is the single most valuable inherited asset — restyled and pointed at Butterbase's Data API instead of axon's local server. Views: **Graph** (heatmap idle state; ripple animation on impact answers — BFS rings light up hop-by-hop with a 120ms stagger; red = uncovered), **Agent theater** (streamed tool-call feed: Cypher shown verbatim, Daytona log tail, diff viewer, PR link), **Memory** (Cognee notes timeline per repo), **Billing** (plan card → Stripe Checkout). Realtime subscription drives all of it. Aesthetic: dark, dense, terminal-adjacent — a tool that reads as built for engineers, not a generic dashboard skin; polish should be allocated proportionally, since the inherited canvas already does the hard visual work.

---

## 11. Provenance & attribution

RIPPLE builds on `harshkedia177/axon` (open source): its Python AST→graph extraction, igraph community detection, and React graph canvas. Everything beyond that is original to this project: the Neo4j port and risk model, both RocketRide pipelines and the Wave-agent toolchain, the Daytona verification loop and snapshots, the Cognee memory design, and the entire Butterbase chassis. This attribution should stay visible in the README and any project description — disclosed reuse of OSS is standard practice, and the value here is the integration and product built on top of it.

---

## 12. Risk register, fallbacks, and roadmap

| Risk | Likelihood | Fallback |
|---|---|---|
| RocketRide Cloud availability/queue issues | Med | Pipelines are portable JSON: self-hosted Docker as understudy, Cloud as endpoint of record |
| `db_neo4j` NL→Cypher flakiness | Med | Node's EXPLAIN-retry (max_attempts=5) + rich `db_description`; the two most common queries also exist as canned Cypher behind a UI toggle |
| Aura free tier lacks GDS | Med | §5.4 ladder — igraph in ingestion (axon code path), scores as properties |
| Daytona quota/latency at load | Low-Med | Snapshots (§8); move to per-request sandboxes via the raw SDK as usage grows |
| Cognee-on-Neo4j config friction | Med | Embedded Kuzu default preserves the memory feature; "same server" simplification is the only thing sacrificed |
| Stripe Connect onboarding friction | Low | Docs' documented alternative: direct Stripe integration in a serverless function + state in app tables — still a Butterbase-native payment flow |
| Butterbase OAuth (GitHub) misconfig | Low | Email/password auth is a one-call fallback; OAuth is an upgrade, not a dependency |
| Scope creep on any single layer | High, by default | Sequence features by user value (§3's scope boundaries); keep the ripple animation, the sandbox-verified fix, and billing as the non-negotiable core loop at every stage |

**Forward roadmap (beyond initial scope):** multi-language ingestion (JS/TS, Go) once the Python path is solid; general "bring your own repo" support with async ingestion queuing; auto-merge for low-risk, fully-verified fixes with team approval gates; team/org RBAC on top of the current per-user model; per-request Daytona sandboxes for concurrent load; richer Cognee `forget` policies (e.g., decay old fragility notes as tests improve); a CLI/CI-bot mode that comments blast-radius risk directly on pull requests.
