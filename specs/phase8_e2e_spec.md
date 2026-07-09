# Phase 8 — End-to-end verification, flagship demo repo, docs

Run after Phases 1–7 are green. Mixed orchestrator/Codex phase.

## 1. Flagship demo repo (the §3 journey needs a real, resonant target)

Ingest `fastapi` (github.com/fastapi/fastapi — the design's own example target;
`OAuth2PasswordBearer.__call__` lives in `fastapi/security/oauth2.py`):
- `python -m ripple.ingest --repo https://github.com/fastapi/fastapi --repo-id fastapi --wipe`
- Verify: Function count in the thousands; `resolve?q=OAuth2PasswordBearer.__call__` finds
  the fqn; blast radius on it returns impacted dependents with sane risk ordering; graph
  endpoint renders within caps.
- Build the sandbox snapshot for fastapi (Phase 4 provider); scoped tests via COVERS for
  the oauth2 blast radius must select security-related test files.
- If fastapi is too heavy for the live fix demo, the fallback flagship is `pallets/itsdangerous`
  (small, clean tests) — but graph/blast/ask must still work on fastapi.

## 2. Full-journey smoke — `scripts/smoke_e2e.sh`

One script, from cold start (docker compose-style bring-up of: both neo4j, cognee, engine,
mcp, api, chassis, pipelines, frontend build) through the §3 journey:
1. login → 2. ingest (miniproj fresh copy) → 3. graph ready+rendered (API-level asserts +
   Playwright canvas check) → 4. ask blast question (ripple animation data present, impact
   + Cypher shown) → 5. fix-it with intent → sandbox-verified diff, iterations ≤3,
   verification.passed true → draft PR skipped-or-url → 6. memory recall in a FRESH P2
   session cites the decision → 7. billing gate flips correctly. Each step asserts and the
   script exits nonzero on failure. Artifacts (screenshots, JSON responses) into
   scripts/artifacts/e2e/.

## 3. Docs

- `README.md` (root): what RIPPLE is (one-liner + §3 journey), architecture diagram (ASCII
  from §4), quickstart (env setup → docker → engine → gateway up → frontend dev), smoke
  test index, **Provenance & attribution (§11)**: built on harshkedia177/axon (link), what
  was inherited (AST→graph extraction, igraph community detection, React graph canvas)
  vs. what's original (Neo4j port + risk model, RocketRide pipelines + Wave toolchain,
  sandbox verification loop, Cognee memory design, chassis, frontend product surface).
- `docs/CLOUD_DEPLOY.md`: the credential checklist + exact swap steps for each layer:
  RocketRide Cloud (ROCKETRIDE_URI=wss://api.rocketride.ai + token; same .pipe JSON),
  Neo4j Aura (neo4j+s:// URI + creds; ripple/memory DBs), Daytona (DAYTONA_API_KEY →
  DaytonaProvider activates; snapshot build commands), Butterbase (chassis/butterbase
  RUNBOOK), Anthropic key (llm_anthropic swap in both pipes), Stripe.
- `docs/OPERATIONS.md`: start/stop everything, where logs live, how to watch RocketRide
  per-node traces, common failure modes learned during the build (engine auth, lane
  routing, MCP timeouts, Cypher parameterization).

## 4. Final review pass

- /code-review level high on the full diff since the scaffold commit; fix confirmed findings.
- Kill leaked containers/processes; `docker ps` clean except the named ripple services;
  final green run of every scripts/smoke_phase*.sh in sequence, then smoke_e2e.sh.
