# Phase 7 Spec — RIPPLE Frontend (graph UI + agent theater)

Context: `RIPPLE_system_design.md` §10 + §3 journey. Lift axon's React graph stack
(`axon/src/axon/web/frontend/` — React 18, Vite, Tailwind 4, sigma.js 3 + graphology,
zustand, ky) into `frontend/` and reshape it into RIPPLE. The inherited GraphCanvas
(~2k LOC, ForceAtlas2, WebGL) is the crown jewel — reuse it, restyle it, repoint it.

Backends (all running locally): chassis http://localhost:8788 (auth, fn endpoints,
realtime WS, billing), read API http://localhost:8787 (graph snapshot, blast, animation,
resolve, stats). One config module (`src/lib/config.ts`) holds both base URLs from
Vite env with these defaults.

## Views (§10)

1. **Graph** (home, per selected repo):
   - Data: GET :8787/repos/{id}/graph. Node size = blast_score (sqrt scale, clamp),
     color = community palette on a dark ground; uncovered functions (covered=false,
     is_test=false) get a red border/halo (the §5.2 product insight). Idle state = this
     risk heatmap.
   - Ask box ("What breaks if I change …?"): typeahead via /resolve; on submit → chassis
     /fn/askRipple (ask mode). While waiting, animate: fetch /blast/animation ring edges
     and light them up ring-by-ring with a 120ms stagger from the target (BFS ripple —
     dim non-involved nodes, pulse the target, color impacted nodes by risk, red for
     tests==0). The returned analysis fills a ranked impact panel: fqn, hops, risk,
     tests, with an "explain" tooltip button → chassis /fn/explainImpact.
   - Cypher transparency: collapsible "Cypher used" block showing cypher_used verbatim
     (monospace, copy button).
   - "Fix it for me" button on the impact panel → intent input → /fn/askRipple mode=fix →
     switches to Agent Theater.
2. **Agent theater**: streamed feed of the run. Data sources: (a) chassis realtime WS
   events for analyses/fixes rows, (b) IF the gateway exposes engine task events relay
   (check services/gateway; if it doesn't, ADD a minimal SSE relay endpoint to the chassis
   that subscribes to the RocketRide client's task events/monitors for the P2 token and
   forwards {node, status, detail} lines — engine per-node tracing is first-class per §6).
   Render: chronological cards — tool name, args summary, Cypher verbatim when present,
   Daytona/sandbox log tail (monospace, autoscroll), the unified diff (syntax highlighted,
   shiki is already a dep), verification badge (green PASSED cmd/red FAILED), PR link.
   Degrade gracefully: with no live events, render the final structured JSON as a
   post-hoc timeline — the view must always work.
3. **Memory**: GET /memory_notes?repo_id= timeline (date-grouped cards, summary text,
   cognee_ref); empty-state copy explaining the memory layer.
4. **Billing**: plan card (current plan, limits from chassis), Subscribe → chassis local
   checkout page (or Stripe redirect if configured), post-subscribe state refresh.
5. **Repo picker / ingest**: list repos (status chips: ingesting → ready with live WS
   updates + progress if events available), "add repo" (URL or local path) → /fn/ingestRepo.
   Dev-login screen (username only) when unauthenticated.

## Aesthetic (§10)

Dark, dense, terminal-adjacent; engineers' tool, not a dashboard skin. Keep axon's layout
bones (panel layout, command palette if cheap). Monospace for all data (fqns, Cypher,
logs, diffs); one accent color for interactive elements + semantic red/green for
risk/verification. Rename branding to RIPPLE («see what breaks before you break it»).
No new heavy UI deps; Tailwind + existing components. Attribution footer: "graph engine
based on axon (harshkedia177/axon)" (§11).

## Build & wiring

- `frontend/` is a standalone Vite app (copy axon's frontend as starting point, keep its
  eslint/tsconfig hygiene). `npm run dev` on 5173 with proxy to 8787/8788; `npm run build`
  must pass typecheck (`tsc -b`) clean.
- Auth: dev-login cookie/JWT from chassis; attach on all chassis calls; 401 → login screen.
- State: zustand stores — repoStore, graphStore (reuse axon's), analysisStore (current
  ask/fix run incl. theater events), memoryStore, billingStore.
- The WS/realtime client must reconnect with backoff and re-subscribe.

## Live smoke — `scripts/smoke_phase7.sh`

Playwright (or the repo's available browser automation) against the real stack (all
services up, miniproj + axon-self ingested):
1. Login → repo list shows axon-self ready.
2. Open graph → canvas renders (>100 nodes drawn — assert via app-exposed
   `window.__ripple_graph_stats` hook), red uncovered nodes present.
3. Ask "What breaks if I change run_pipeline?" → ripple animation runs (assert the
   animation state flag), impact panel fills, Cypher block non-empty.
4. Fix-mode on miniproj with a simple intent → theater shows a verification badge
   green within 240s, diff rendered.
5. Memory tab shows the note created by the fix. Billing tab renders plan card.
6. Screenshot each view to scripts/artifacts/ for human review. Exit nonzero on failures.

## Quality bar
Typecheck clean, no `any` on new code, no dead axon views left routed (remove Cypher
console? NO — keep it, repointed at :8787's read-only endpoints if trivial, else drop it
cleanly). Do not modify axon/ in place — copy. No git commits.
