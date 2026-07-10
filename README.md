# RIPPLE

RIPPLE is a code-change safety copilot. Point it at a Python repository, ask what could
break if a function changes, and it maps the ripple effect through callers, imports, and
tests. When asked to make a fix, it drafts the smallest patch and runs the relevant tests
inside a disposable cloud sandbox before it reports success.

In plain English: RIPPLE does not just say a change *looks* right. It shows what depends on
that change and produces a test-backed proof that the proposed fix works.

## Why It Is Useful

- **See hidden impact:** Neo4j turns source relationships into a queryable code graph.
- **Get proven fixes:** Daytona runs each patch in an isolated sandbox and replays the final
  diff so the proof matches exactly what RIPPLE returns.
- **Remember decisions:** Cognee stores verified decisions and fragile areas per repository,
  so a fresh session can warn about lessons learned earlier.
- **Keep the agent accountable:** RocketRide orchestrates the planning and tool calls, while
  graph writes and test claims remain behind deterministic application checks.
- **Inspect the evidence:** Results include the impact list, Cypher query, canonical diff,
  test command, log tail, and optional draft PR URL.

### Agent Context and Token Savings per Fix

| Repository size | Conventional agent input | RIPPLE input | Tokens saved | Context reduction |
|---|---:|---:|---:|---:|
| Small (up to 50k LOC) | 20k tokens | 6k tokens | 14k tokens | 70% |
| Medium (50k-250k LOC) | 60k tokens | 12k tokens | 48k tokens | 80% |
| Large (250k+ LOC) | 180k tokens | 24k tokens | 156k tokens | 87% |

The expected reduction comes from replacing broad repository context with the exact target,
ranked callers, imports, relevant tests, and recalled decisions. This also leaves more of the
model's context window available for reasoning about the patch itself.

### Test-to-Pass Time Savings per Fix

| Repository size | Full suite per attempt | Conventional time (3 runs) | RIPPLE scoped verification | Time saved | Reduction |
|---|---:|---:|---:|---:|---:|
| Small | 4 min | 12 min | 4 min | 8 min | 67% |
| Medium | 15 min | 45 min | 10 min | 35 min | 78% |
| Large | 45 min | 135 min | 24 min | 111 min | 82% |

RIPPLE's estimate includes scoped test runs plus sandbox and final proof-replay overhead. It
does not assume tests become faster; it saves time by running the tests connected to the
blast radius during iteration instead of repeatedly running everything.

### Expected Savings over 100 Fixes

| Repository size | Agent input tokens saved | Test-to-pass time saved |
|---|---:|---:|
| Small | 1.4 million | 13.3 hours |
| Medium | 4.8 million | 58.3 hours |
| Large | 15.6 million | 185 hours |

For a typical medium repository, the working estimate is **48k fewer input tokens and 35
minutes less test waiting per verified fix**.

## How It Works

1. RIPPLE parses a Python repository and writes functions, files, calls, imports, tests, and
   risk scores into Neo4j.
2. A RocketRide Wave agent recalls relevant Cognee memory, queries the graph, and plans the
   answer or fix.
3. Fixes are converted into a canonical unified diff and sent to Daytona.
4. Daytona applies the diff, runs graph-selected tests, and independently replays the proof.
5. Only a passing, request-bound result is returned. The verified decision is then saved to
   Cognee and mirrored to SQLite for a fast memory timeline.

## Current Scope

The implemented backend supports Python ingestion, blast-radius queries, verified fixes,
draft-PR provenance, concurrent sandbox cleanup, and authenticated cross-session memory.
The full product UI and hosted Butterbase chassis remain the next deployment phase.

## Quick Verification

Requirements: Python 3.12+, Docker/Colima, `uv`, and credentials in the ignored root `.env`.
No local language model is installed or required.

```bash
# Unit and contract tests
PYTHONPATH=core/src:services/gateway/src:services/ripple-mcp/src \
  .venv/bin/pytest -q core/tests services/gateway/tests services/ripple-mcp/tests

# Live graph + RocketRide + Daytona proof
DOCKER_HOST=unix://$HOME/.colima/default/docker.sock \
  RIPPLE_PHASE4_NEO4J_URI=bolt://localhost:17687 \
  bash scripts/smoke_phase4.sh

# Live authenticated Cognee remember/recall/improve/forget
DOCKER_HOST=unix://$HOME/.colima/default/docker.sock bash scripts/smoke_phase5.sh
```

The detailed architecture and phase contracts are in
[`RIPPLE_system_design.md`](RIPPLE_system_design.md) and [`specs/`](specs/).

## Provenance

RIPPLE builds on the open-source `harshkedia177/axon` parser and graph extraction work.
The Neo4j mapping and risk model, RocketRide pipelines, Daytona verification contract,
Cognee memory integration, gateway, and MCP tools are implemented in this project.

## Future Phases

- **Phase 6 — Product chassis:** Add the user-facing application backend: sign-in,
  per-user repository data, saved analyses and fixes, realtime updates, rate limits, and
  plan/billing controls. A local FastAPI version will keep development fully runnable,
  while a Butterbase deployment kit will make the production swap mechanical once its
  credentials are available.
- **Phase 7 — Frontend:** Turn the backend into the complete RIPPLE experience. Engineers
  will see the code graph ripple outward from a changed function, inspect ranked risks and
  exact Cypher, watch sandbox tests and diffs in an agent theater, revisit remembered
  decisions, and manage repositories and billing from one dark, work-focused interface.
- **Phase 8 — Production proof:** Exercise the whole journey against a flagship repository,
  run one cold-start end-to-end smoke from login through verified fix and fresh-session
  memory recall, capture demo artifacts, finish cloud and operations documentation, and
  complete a final security, cleanup, and code-review pass.
