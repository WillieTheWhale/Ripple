# Phase 4 Spec — Sandbox Verification Loop ("fix it for me", the proof engine)

Context: `RIPPLE_system_design.md` §8 + §6.2 fix-mode. Phases 1–3 done: graph in Neo4j,
blast API, P2 ask-mode agent live on the local RocketRide engine, ripple-mcp server
(`services/ripple-mcp`) already exposes ingest/blast tools to the agent.

Design constraint honored here: the agent's *contract* is Daytona-shaped (upload_file /
run_command / apply patch / run scoped tests). We implement a provider abstraction:
`DaytonaProvider` (uses the `daytona` SDK, activates when DAYTONA_API_KEY env is set) and
`LocalDockerProvider` (default locally: docker containers standing in for Daytona sandboxes,
same semantics incl. snapshot-style prebuilt images). When the user supplies a Daytona key,
it's a config flip — no agent or pipeline changes (§12 fallback ethos).

## Deliverables

### 1. `core/src/ripple/sandbox/` — provider abstraction

- `base.py`: `Sandbox` protocol: `upload_file(path, content)`, `download_file(path) -> str`,
  `run_command(cmd, cwd=None, timeout=300) -> {exit_code, output, truncated}`, `destroy()`.
  `SandboxProvider` protocol: `create(repo_id) -> Sandbox`, `ensure_snapshot(repo_id,
  repo_path_or_url) -> str` (prebuilt image/snapshot with repo cloned + deps installed +
  `pytest --collect-only` warmed — §8 snapshot strategy).
- `local_docker.py`: implements both using the docker CLI (subprocess; no docker SDK dep
  needed). Snapshot = docker image `ripple-sbx-<repo_id>` built from a generated Dockerfile:
  `python:3.12-slim` + git + repo copied in at `/work/repo` + `pip install -e '.[test,dev]'`
  (fall back through `[test]`, `[dev]`, plain `-e .`, then `pip install -r requirements*.txt`,
  then pytest only — detect what the repo offers) + `pip install pytest coverage` + warm
  collect. `create()` = `docker run -d ripple-sbx-<repo_id> sleep infinity`; commands via
  `docker exec`. `destroy()` = `docker rm -f`. Container names `ripple-sbx-run-<uuid8>`.
- `daytona_provider.py`: same interface via the `daytona` PyPI SDK (create from snapshot,
  `sandbox.process.exec`, `fs.upload_file`); import lazily so the dep is optional; concise
  and honest — it activates only with DAYTONA_API_KEY set, and mark it clearly untested-live
  in a module docstring until a key exists.
- `verify.py` — the verification loop as a pure function over a Sandbox:
  `verify_patch(sandbox, diff_text, test_paths: list[str], max_output=8000) -> VerifyResult
  {applied: bool, passed: bool, exit_code, cmd: str, log_tail: str, duration_ms: int}`:
  upload diff to `/work/patch.diff` → `git apply --check` then `git apply` (report apply
  failure distinctly) → `python -m pytest <paths> -q --maxfail=5 -p no:cacheprovider
  --timeout=60` (install pytest-timeout in snapshot; if `--timeout` unsupported at runtime,
  degrade gracefully by omitting it) → parse result. Also
  `scoped_tests(driver, repo_id, fqns: list[str]) -> list[str]`: DISTINCT test file paths
  from COVERS edges into the impacted fqns (this is the Neo4j×sandbox payoff — run only the
  blast radius's tests), capped at 20 files, falling back to the repo's full `tests/` dir
  if empty.

### 2. ripple-mcp: new tools (extend `services/ripple-mcp`)

REMINDER (from Phase 3): tool_mcp_client has a 20s per-call timeout — snapshot builds,
verify runs, and PR pushes MUST be job-shaped (`{job_id}` + `job_status` polling, reusing
Phase 3's job registry). `sandbox_run`/upload/download of small files can stay sync (bound
command timeout at 15s for sync calls; agent should use job-shaped `verify_patch` for tests).

- `prepare_sandbox(repo_id)` → job-shaped: ensures snapshot (slow docker build on first
  run), creates sandbox; job result carries sandbox_id. Registry of live sandboxes in the
  MCP process; auto-destroy on 15min idle and on process exit.
- `sandbox_run(sandbox_id, command, cwd=None)` / `sandbox_upload(sandbox_id, path, content)`
  / `sandbox_download(sandbox_id, path)` — direct passthroughs (agent-visible).
- `read_function_source(repo_id, fqn)` → file_path/start/end lines from Neo4j + source text
  from the ORIGINAL repo checkout (workdir path stored on the Repo node at ingest — add
  `root_path` property in a tiny Phase-1 amendment if missing; re-ingest axon-self if you
  change ingest).
- `scoped_tests(repo_id, fqns)` → the COVERS-derived test list.
- `verify_patch(repo_id, sandbox_id, diff_text, fqns)` → job-shaped; job result is
  VerifyResult + the scoped test list used.
- `open_draft_pr(repo_id, branch_name, diff_text, title, body) -> {pr_url}`:
  applies the diff on a new branch of a working clone, commits, pushes, opens a DRAFT PR
  via `gh` CLI (token from env GH_TOKEN/ROCKETRIDE_GITHUB_TOKEN). Gate the entire tool
  behind env `RIPPLE_PR_REPO` (owner/name of the target GitHub repo) — when unset, return
  {skipped: true, reason} instead of failing. Never force-push; branch names
  `ripple/fix-<uuid8>`.

### 3. P2 pipeline: enable fix-mode

Update `pipelines/ripple_ask.pipe` agent instructions for mode=fix (§6.2 policy, concrete):
1) blast_radius + read_function_source on the target; 2) draft a minimal unified diff
(git format, a/ b/ prefixes, correct hunk headers — read the exact current source first);
3) prepare_sandbox; 4) verify_patch with the impacted fqns; 5) if failed, read log_tail, fix
the diff, retry (max 3 total verify_patch calls); 6) only when passed=true: open_draft_pr
(if the tool reports skipped, say so) and include in the final JSON:
{mode:"fix", diff, verification:{cmd, passed, log_tail_last_20_lines}, pr_url|null,
impact, cypher_used, iterations}. NEVER claim verification you did not run; if all 3
attempts fail, return passed:false with the last log and your analysis. Keep ask-mode
instructions intact.

### 4. Live smoke — `scripts/smoke_phase4.sh` (must run green)

Deterministic layer first, agent layer second:
1. Python-level: build snapshot for `miniproj` (fixture repo — give it a real pyproject with
   pytest so the snapshot builds; add one if Phase 1's fixture lacks it, updating fixture
   tests), create sandbox, verify a KNOWN-GOOD handwritten diff (e.g. change a return value
   miniproj's tests assert on, with the test updated in the same diff → green), and a
   KNOWN-BAD diff (breaks a covered function → tests fail) — assert passed=true/false
   respectively and that scoped_tests returned the right test file. This proves the loop
   without agent nondeterminism.
2. Agent-level (P2 fix-mode, repo_id miniproj): intent like "make <some miniproj function>
   handle <specific edge case>; update tests accordingly" — assert: HTTP 200, JSON parses,
   verification.passed == true, diff non-empty, iterations <= 3. Cross-check by running the
   returned diff through verify_patch directly (same sandbox class) and asserting it passes —
   trust but verify the agent's claim.
3. Cleanup: no leaked ripple-sbx-* containers (docker ps check in the script).

## Quality bar
Typed, logged, no dead code; sandbox ops must never touch the host repo checkout (only
clones/images). Do not modify axon/ or engine files. No git commits.
