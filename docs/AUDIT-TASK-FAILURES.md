# Audit: "Every Task Fails" — Root Causes and Fixes

Date: 2026-09-09. Scope: why submitting *any* task to Forge (cockpit,
API, or Supervisor) ended in failure, and what changed so failures are now
diagnosed, recoverable, or gone.

Test baseline before the fix: **1692 passed, 3 skipped** — the suite was
green because every test injects a scripted model. The failure only shows
with the real default fabric, which is exactly what operators use.

## The failure chain (primary cause)

Reproduced with a default `ModelFabric.from_defaults()` and no Ollama
server running:

```text
submit task
  -> ControlPlane._execute_run -> Supervisor.run (fabric = defaults)
  -> fabric routes to ollama/llama3.2, which raises (connection refused)
  -> failover lands on local-fallback, which returns {"changes": {}} with success=True
  -> CoderAgent._parse_changes: zero changes -> ValueError("Model proposed no changes")
  -> Supervisor: ROLLBACK -> run FAILED, error "Model proposed no changes"
```

Out of the box Forge registers exactly two candidates: a real local model
(`ollama/llama3.2`) that only works when the Ollama server runs **and** the
model is pulled, and `local-fallback`, a deterministic placeholder that
honestly refuses to synthesize code. On a typical machine (no Ollama, no
`OPENAI_API_KEY`) *every* task deterministically failed at the MODEL stage
— then rolled back, leaving nothing behind and explaining nothing.

## Findings (ranked)

1. **No working model out of the box, silent fallthrough (critical).**
   Routing correctly tries Ollama first and the placeholder last
   (`forge/models/router.py`), but the placeholder answers `success=True`
   with an empty change set, so the coder cannot distinguish "the model
   chose to do nothing" from "there is no model". The user-visible error
   was `Model proposed no changes`.
2. **Unactionable errors (high).** Neither the supervisor outcome, the
   cockpit `task.failed` event, nor `forge models test` (which reported
   `success=True` from the placeholder) told the operator *why* or *how
   to fix it*.
3. **No readiness/pre-flight gate (high).** Tasks were accepted, queued,
   and checkpointed, then failed — instead of failing fast with a
   diagnosis before doing anything.
4. **No `forge run` CLI (medium).** Terminal users could only `plan` (a
   static step list), `analyze`, or `serve` — there was no way to execute
   a task from the command line at all.
5. **Fragile model-output parsing (medium).** Small local models wrap the
   required JSON in prose ("Here is the change: {...}") or emit trailing
   commas. `_parse_changes` did `json.loads` on the whole payload after
   fence-stripping only, so any wrapping failed the entire task with
   `Model returned invalid JSON` and no recovery attempt.
6. **Assisted-mode approval stalls (medium, by design).** Default sessions
   are `assisted`: every write blocks on an approval (`WAITING_APPROVAL`).
   If the operator misses the approval prompt, the task looks stuck. This
   is a safety feature, not a bug — but it needed surfacing, not silence.
7. **Misleading `forge models test` (low).** A trivial prompt answered by
   the placeholder reported success, suggesting the model layer was fine.

## Fixes implemented

| # | Fix | Location |
|---|-----|----------|
| 1–3 | New readiness module: `check_fabric_readiness` (registry + live Ollama probe with short timeouts, never raises, never leaks secrets), `fabric_has_real_model`, `is_fallback_response`, `describe_no_model_error` (actionable message with ranked remediations) | `forge/models/readiness.py` |
| 1–2 | Coder detects placeholder-refusal (fallback attribution **or** refusal-marker payload) and returns the diagnosis; empty changes from a *real* model keep the legacy message | `forge/agents/coder.py` |
| 3 | Supervisor pre-flight gate: fail fast at MODEL stage when no non-fallback model is registered (`model_unavailable` event + diagnosis) | `forge/core/supervisor.py` |
| 4 | `forge run "requirement"` (fabric from `.forge/models.yaml`/env/flags, `--mode`, `--approve`, `--json`, fail-fast exit 2 when fallback-only) and `forge doctor` (env + fabric diagnosis, exit 0/1, `--json`, `--offline`) | `forge/cli.py` |
| 5 | Robust JSON recovery: balanced-brace extraction (longest-first, string-aware) + single trailing-comma repair pass; `extracted_from_prose` recorded in metadata; one bounded repair re-prompt on still-invalid output (`repair_attempted`), honest failure afterwards | `forge/agents/coder.py` (`loads_model_json`) |
| 6 | Approvals surfaced prominently in the new desktop app; setup guide documents modes and `WAITING_APPROVAL`; approval semantics unchanged (fail-closed, no self-approval) | `forge/desktop_app/` |
| 2,7 | `GET /api/v1/models/readiness` + `ControlPlane.model_readiness()` for cockpit/desktop; `forge doctor` replaces `models test` as the health signal | `forge/api/routes_views.py`, `forge/control/control_plane.py` |

Safety invariants preserved: placeholder still never invents code;
validation after JSON recovery is identical to the verbatim path; repair
re-prompt is a single extra model call, bounded, and reported; pre-flight
never touches the network (static registry check); the live probe only
runs on paths that already failed or were explicitly asked for
(`doctor`, readiness endpoint).

## Verified behavior after the fix

* `forge doctor` with no Ollama → `NOT READY`, names `ollama serve` +
  `ollama pull llama3.2`, exit 1.
* `Supervisor.run(...)` with defaults and no Ollama → `accepted=False`,
  error explains the placeholder refusal, names the broken link
  (unreachable endpoint), and points at `forge doctor`.
* `forge run` with a fallback-only config → exit 2 with the diagnosis on
  stderr, no doomed pipeline executed.
* Prose-wrapped and trailing-comma model payloads now apply successfully
  (`extracted_from_prose=True`); still-invalid output fails honestly after
  exactly one repair attempt.
* New suites: `tests/test_task_failure_recovery.py` (22 tests),
  `tests/test_desktop_app_backend.py` (13), `tests/test_desktop_app_gui_stub.py` (8).
  Full suite: see commit message for the final count.

## Sweep 2: "fix it all" (2026-09-10)

A second full-repo pass after the audit fixes landed:

* **Lint clean (121 → 0 pyflakes findings).** Removed dead imports across
  `forge/` and `tests/` (including `# noqa`-shielded ones pyflakes still
  reports), two placeholder-less f-strings, one dead local, and one
  duplicated import. The intentional `import modal` presence-check was
  rewritten as `importlib.util.find_spec` (same behavior, no dead import).
* **Shadowed tests unshadowed.** `tests/test_agent_registry.py` ended in a
  copy-pasted tail that redefined 4 tests; the duplicates were deleted so
  every collected test is unique.
* **Honest `forge models test`.** A placeholder answer now prints
  `success=False` + a WARNING pointing at `forge doctor` (text) and sets
  `used_fallback: true` + `warning` (JSON). Previously it reported
  `success=True` with no model reachable.
* **Demo launcher repaired.** The terminal ALLOW rule pinned an executable
  and args that could never match a real run; it is now built from the
  actual test command (`sys.executable` basename + base pytest args), so
  the demo's test stage is authorized while everything else still falls
  through to approvals. The launcher also binds loopback (`127.0.0.1`)
  instead of `0.0.0.0` — local-dev auth has no passwords and must never
  listen on all interfaces.
* **`forge run` assisted-mode gate.** Assisted mode without `--approve`
  has no satisfiable approver in a terminal run, so it now fails fast
  (exit 2) with guidance instead of running a doomed pipeline. `--force`
  bypasses both pre-flight gates when the operator wants the raw failure.
* **CLI `--json` positions fixed.** `forge models test --json` errored and
  `forge models --json test` silently dropped the flag (argparse
  subparser-default overwrite); both orders now work.
* **Clean test output.** The two known third-party deprecation warnings
  (starlette TestClient shim, anyio alias) are filtered in
  `pyproject.toml`; Forge's own warnings are never filtered.
* **Live-model validation attempted, correctly out of scope.** This
  sandbox's network allowlists PyPI only (Ollama/HuggingFace/OpenAI are
  unreachable), so the 3 opt-in live-model skips stay skipped here by
  design. On a machine with Ollama, run
  `FORGE_LIVE_MODEL_TESTS=1 python -m pytest tests/test_ollama_live.py
  tests/test_ollama_autonomous_e2e.py -q` for the real-model proof.
* **Flaky checkpoint test hardened.** `test_checkpoints_listed_for_task`
  asserted the checkpoint list immediately after the RUNNING flip, but the
  pre-run checkpoint registers just after that flip — one transient
  `assert []` was observed. The test now polls (bounded) for the
  checkpoint instead of relying on the racy single read.

## Sweep 3: agent force-fix (2026-09-10)

Full agent inventory: 6 core agent classes (coder, debugger, reviewer,
tester, stage adapter, callable adapter) plus runner/factory
infrastructure; a 5-agent supervisor roster; an 11-worker orchestration
team (planner, architect, researcher, coder, tester, debugger, reviewer,
security, performance, documentation, git); the AI council; and the
external-AI collaboration session. Audit result: most were real, four
were stubs or toys. Fixed forcefully:

* **`TesterAgent` was a 5-line stub** (`name` + `describe()`, zero
  usages). It is now a real bounded pytest runner (whole suite or
  targeted `tests_to_run`, paths confined to the repo, timeout,
  pass/fail/no-tests verdicts, truncated output).
* **Supervisor roster was 60% canned text.** Reviewer/tester/security
  were one-line lambdas returning static strings. They now execute real
  gates: the deterministic ReviewGate over request context (+ optional
  model findings), the real TesterAgent, and the verification
  pipeline's security gate — via testable `build_reviewer_executor` /
  `build_security_executor` builders.
* **Orchestration architect was keyword matching** (`if "api" in
  requirement` canned proposals). It is now model-backed (capability
  `planning`, repo inventory in the prompt, strict JSON parsing) with
  the old heuristic kept only as a *labeled* fallback
  (`source: heuristic-fallback` + warning naming the cause).
* **Orchestration reviewer was keyword counting.** It now runs the real
  deterministic ReviewGate over bounded repo files and merges optional
  model review (`model_reviewed` flag in the output).
* **ReviewGate had a dangerous blind spot**: `os.system` / `os.popen` /
  `sh -c` / `bash -c` passed as APPROVE (only `eval`/`subprocess shell=`
  were caught). Added `os-exec` / `shell-c` / `bash-c` HIGH rules
  mirroring the verification pipeline.
* **AI council gained real members.** `FabricCouncilMember` deliberates
  through the Model Fabric (`simulation: false`, abstains honestly with
  cause when no model serves it). The default stays explicitly
  simulated; the engine's `simulation` flag is now computed from actual
  member opinions instead of hardcoded `True`.

Left as-is (verified honest, not broken): the labeled council/collab
simulations, `AgentRunner`'s refusal to run definitions without a bound
executor, training/evolution/selector/validator/governance/memory/skills
(pure logic + real API clients, fully tested).

## Sweep 4: media integrations + power-ups + flake hunt (2026-09-10)

* **Blender (new, `forge/media/blender.py`).** Procedural 3D scenes
  (meshes, text, lights, tracking camera, spin/drift animation,
  EEVEE/Cycles/Workbench) validated against strict bounds, compiled to
  a *static* `bpy` program fed by a sidecar JSON file (scene content
  can never inject code; engine/socket names probed for 3.x/4.x), and
  executed via headless `blender --background` with timeouts. Missing
  binary reports `available: False` with install guidance while still
  validating and emitting the script. Wired as `BlenderTool`
  (agent-callable) and `forge blender check|example|render` (CLI).
* **Higgsfield (new, `forge/media/higgsfield.py`).** Real stdlib-only
  client for the documented async API (`Authorization: Key id:secret`,
  submit -> queued -> poll -> terminal, cancel, 7-day outputs).
  Credentials from `HF_API_KEY_ID` / `HF_API_KEY_SECRET` (plus
  `HIGGSFIELD_*` aliases), never logged; FastAPI error envelope mapped
  to typed errors; GET-only retries with backoff/jitter inside a
  deadline; POST never auto-retried; `X-Correlation-ID` captured.
  Wired as `HiggsfieldTool` (minimal) and
  `forge higgsfield status|submit|get|cancel|download` (CLI).
* **Beyond-a-GPT-wrapper power-ups.** Orchestration researcher adds
  model synthesis over its repo inventory (labeled stats-only
  fallback); documentation worker now reports real AST docstring
  coverage (parsed, never executed) with worst-files and unparseable
  lists. Both keep their previous output keys.
* **Flake hunt.** Full suite caught
  `test_pause_resume_cancel_queued_task` racing the worker: pausing a
  RUNNING task returns status RUNNING (pause is a *request* effective
  at the next stage boundary), breaking the test's `== "PAUSED"`
  assert, and canceling an already-terminal task raises. The test now
  accepts every legal interleaving instead of assuming it wins the
  race.

Tests: 1801 passed (42 new: 18 Blender, 21 Higgsfield over a real
localhost HTTP stub, 3 worker power-ups), 3 skipped (opt-in
live-model), 0 warnings. Blender/Higgsfield live E2E is impossible in
this sandbox (no Blender binary; only PyPI egress), so the suites
prove the real plumbing against stub binaries/servers and assert the
honest unavailable-paths.

## Sweep 5: fix all fakes (2026-09-10)

Every remaining fake in production code was hunted down; each is now
real or was already honest-by-design (kept, noted below):

* **Agent router misread plain English (fixed).**
  `TaskRequirementExtractor` used substring matching, so "legitimate"
  routed to the git agent, "latest news" to testing, "address" to
  coding, "prefix" to debugging. It now matches whole-word tokens
  plus real inflections (`fixing`, `committed`, `branches`) and
  multi-word phrases — misroutes gone, intended routes unchanged.
* **Computer-use had no real hands (fixed).** `FakeDesktopProvider`
  was the *only* desktop provider in the tree. New
  `forge/desktop/local_provider.py` drives the real machine: system
  info, process table/signals (`/proc`, `ps`, `tasklist`), confined
  file access, and shell-free process launch are always real; GUI
  (screenshots, windows, mouse, keyboard, clipboard) executes through
  detected backends (scrot/grim/screencapture, xdotool,
  xclip/xsel/wl-clipboard/pbcopy) and returns structured
  `unsupported` naming the missing tool otherwise. Screenshots are
  parsed PNGs (IHDR dimensions verified); there is deliberately no
  OCR and `read_screen` says so. Self-destructs (PID 1, own PID) are
  refused. The control-plane default stays the fake for safety;
  operators opt into real with
  `ControlConfig(desktop_provider=LocalDesktopProvider())`.
* **Fragile fake-detector (fixed).** `DesktopBridge.simulate()`
  branched on the literal class-name string
  `"FakeDesktopProvider"`; providers now carry `name`/`simulation`
  attributes and the bridge gates on the flag.
* **Vision API lied (fixed).** `/vision/capabilities` hardcoded
  `simulated_only: True` even with the real OpenAI vision provider
  configured. New `ControlPlane.vision_capabilities()` computes it
  from the active provider (plus an `active_provider` field); the
  route serves it.

Kept as honest-by-design (real counterparts exist, labels explicit):
simulated vision/voice/collaboration/council defaults, the offline
model placeholder (refuses, never fakes), `MockProvider`
(test-only), and `MockBrowser`/`MockNetwork`/`MockDesktop` (A33
permission-test fixtures with zero production usages).

Tests: 1821 passed (20 new: 16 local provider incl. real process
signals + PNG parsing through stub backends, 3 extractor, 1 vision),
3 skipped (opt-in live-model), 0 warnings.

## What operators should do

1. Run `forge doctor`. Fix whatever it lists (usually: install Ollama,
   `ollama serve`, `ollama pull llama3.2` — or set `OPENAI_API_KEY`).
2. Prefer `forge run --mode autonomous --approve` for trusted local work,
   or stay in `assisted` and approve each write in the cockpit/desktop app.
3. If a task fails, read `error` first: it now names the cause and the fix.
