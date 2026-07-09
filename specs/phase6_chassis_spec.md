# Phase 6 Spec — Product Chassis (local Butterbase understudy + real-Butterbase deploy kit)

Context: `RIPPLE_system_design.md` §7. Butterbase (docs.butterbase.ai) is the production
chassis: Postgres+RLS, GitHub OAuth, serverless fns, realtime, Stripe billing, static
hosting. Creating a Butterbase account requires a human browser signup — no credentials
exist yet. So this phase ships BOTH:
(a) `services/chassis/` — a local FastAPI implementation of the exact same product surface
    (the app must be fully usable end-to-end locally), and
(b) `chassis/butterbase/` — the declarative artifacts + a deploy runbook so that when the
    user provides a Butterbase app_id + API key, deployment is mechanical (schema JSON,
    function sources in Butterbase's TS serverless format, RLS notes, Stripe plan calls).
The chassis API shape should stay close to Butterbase's documented Data API conventions
(resource-per-table REST + auth endpoints) so the frontend swaps backends via one base-URL
config.

## Deliverables

### 1. `services/chassis/` (FastAPI, port 8788)

- SQLite at `chassis/ripple_local.db` (reuse/extend `core/src/ripple/chassis_db.py` from
  Phase 5). Tables per §7.1: users, repos(id, owner_user_id, gh_url, name, status,
  node_count, edge_count), analyses(id, repo_id, user_id, mode, question, impact_json,
  risk_top, cypher_json, created_at), fixes(id, analysis_id, diff_text, verified, test_cmd,
  log_tail, pr_url, daytona_ms), memory_notes (exists from Phase 5).
- Auth: dev-mode session auth — POST /auth/dev-login {username} issues a signed cookie/JWT
  (env RIPPLE_DEV_AUTH=1 default on). GitHub OAuth optional behind env (GITHUB_OAUTH_CLIENT_ID/
  SECRET) using the standard authorize/callback flow; both produce the same user row.
  Per-user row scoping enforced in every query (the RLS understudy).
- Product endpoints (auth required):
  - POST /fn/ingestRepo {gh_url|path, repo_id?} → plan-limit check (see billing) → repos row
    status=ingesting → POST to P1 webhook → on response update status/counts → realtime event.
  - POST /fn/askRipple {repo_id, mode, question, intent?} → entitlement check → rate limit
    (10/min/user via in-proc token bucket; the KV understudy) → POST P2 webhook (180s
    timeout) → persist analyses row (+ fixes row when mode=fix with verification) →
    realtime event → return the structured result.
  - GET /repos, /analyses?repo_id=, /fixes?analysis_id=, /memory_notes?repo_id= (RLS-scoped).
  - WS /realtime — broadcasts {table, action, row} on every insert/update (the Butterbase
    realtime understudy). The UI subscribes to drive live updates (§4 control flow).
  - POST /fn/explainImpact {impact_json} → the AI-gateway feature (§7.3): calls Gemini
    directly (key from env) with a compact prompt to produce the plain-English tooltip
    text. Keep it one short completion; this is deliberately a SEPARATE AI path from the
    P2 agent (mirrors Butterbase AI gateway usage).
- Billing (§7.3): plans table seeded with "Ripple Pro" 900¢/mo (private repos, unlimited
  repos, priority). Free tier: 2 public repos max, ask-only after 20 asks/day. Local mode:
  POST /billing/subscribe flips the user's plan (fake checkout page served by chassis that
  says LOCAL DEV — no card), GET /billing/subscription returns state; gate ingestRepo
  (repo count + private) and fix-mode on plan. If STRIPE_SECRET_KEY env exists, use real
  Stripe Checkout in test mode instead (create price on boot if missing, webhook endpoint
  /billing/webhook with signature check). Entitlement logic identical either way.

### 2. `chassis/butterbase/` deploy kit

- `schema.json` — §7.1 tables in Butterbase declarative JSON schema format (follow
  docs.butterbase.ai conventions as far as the public docs show; keep a comment noting
  fields to verify against the MCP scaffolder on first real deploy).
- `functions/` — ingestRepo.ts, askRipple.ts, stripeSuccess.ts, nightlyMemify.ts written
  for Butterbase's TS serverless runtime (HTTP handlers + one cron), mirroring the chassis
  logic 1:1 (webhook URLs/keys from Butterbase env/KV).
- `RUNBOOK.md` — exact steps once credentials exist: MCP-server scaffold commands, RLS
  one-calls per table, GitHub OAuth app setup, Stripe Connect plan creation
  (`POST /v1/{app_id}/billing/plans` per docs), frontend deploy, env var wiring to
  RocketRide Cloud webhooks. Include a "credentials needed" checklist.

### 3. Live smoke — `scripts/smoke_phase6.sh`

1. Start chassis (+ deps: neo4j, engine, mcp, pipelines via gateway up).
2. dev-login user A → ingest miniproj via /fn/ingestRepo (local path) → poll /repos until
   status=ready with node_count>0.
3. /fn/askRipple ask-mode → 200; analyses row exists; a WS client connected before the ask
   received the realtime insert event (assert in-script with a python WS listener).
4. RLS: dev-login user B → GET /repos and /analyses must NOT show user A's rows.
5. Billing gate: as free user B try /fn/ingestRepo on a 3rd repo (seed two rows first) →
   402/403; subscribe → succeeds. fix-mode gated the same way.
6. /fn/explainImpact returns non-empty prose for a sample impact list.
7. Teardown chassis; exit nonzero on any failure.

## Quality bar
Typed, logged, secrets only via env. Keep the chassis honest: any place it diverges from
documented Butterbase behavior, add a `# BUTTERBASE:` comment stating the real equivalent.
Do not modify axon/ or engine files. No git commits.
