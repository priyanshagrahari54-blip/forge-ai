# Forge AI

Forge is a repository-scoped software-engineering runtime. It combines repository intelligence, context selection, model routing, permissioned tools, bounded debugging, independent verification, checkpoints, and explicit Git staging.

## Execution architecture

1. `RepositoryIntelligence` indexes symbols, dependencies, architecture, runtime commands, and test mappings.
2. `AgentContextBuilder` selects relevant source, dependency, and test context under a token budget.
3. The centralized `ModelFabric` routes through a capability/context/complexity-aware `FabricRouter` over the model and provider registries, scoring reliability, latency, cost, free/local status, health, and availability, with a deterministic fallback ladder. Local/Ollama providers are first-class; paid APIs are optional.
4. `CoderAgent` asks the selected provider for a structured change (`changes: {path: content}`), validates it, and writes only through the permissioned runtime. A caller does not need to supply changes.
5. `TestDebugLoop` runs the repository test command, gives captured stdout/stderr and context to a model, applies its bounded repair proposals through `ToolRuntime`, records telemetry, and reruns tests.
6. `VerificationPipeline` runs tests, compilation/build, configured Ruff/mypy checks when declared, secret/dangerous-operation scanning, and an independent changed-file review. A failed gate prevents acceptance.
7. `CheckpointManager` snapshots the exact pre-change files and restores only files changed by the candidate; it does not use `git reset --hard` and leaves unrelated files alone.
8. `GitTool.stage_files` requires an explicit safe file list and rejects Forge runtime state. Autonomous commits never use `git add .`.

The self-development loop A26-A30 follows analyze → candidate → model-selected implementation → checkpoint → code → debug → verification → measurable benchmark → compare → commit or restore, and records history under `.forge/self/history/` (history is not committed).

## Autonomous Engineering Core (A32)

The `Supervisor` is the controller of a real closed-loop engineering engine (A32). Every autonomous task runs:

```text
requirement → understand → inspect → plan → select agents → route model
→ generate implementation → validate ChangeSet → authorize (policy gate)
→ apply changes (controlled layer) → run tests → diagnose failures
→ repair → retest → independent review → security → build/lint
→ benchmark → acceptance decision → checkpoint → commit approved files
```

- **Controlled ChangeSet engine** (`forge.tools.change_applier`): the single layer where model output becomes repository writes. It validates paths (relative, no `..`/`.git`/`.forge`/backslashes), rejects secrets/credentials/`.env`/oversized/invalid-Python content, enforces optional old-content/hash guards, writes through the permissioned `ToolRuntime`, records every changed path, and checkpoints before the first write when a `CheckpointManager` is supplied. `dry_run()` validates proposals with zero writes, `fingerprint()` identifies them deterministically, failures are structured (`code`/`path`/`message`), and `delete` is supported only behind an explicit operator gate (`allow_delete` plus approval) — model-proposed deletes stay rejected.
- **Permission/policy gate** (`forge.security.policy_gate`): every change is authorized *before* modification with the operation, path, tool, risk, and requested capability visible, returning `ALLOW` / `DENY` / `REQUIRE_APPROVAL`. Modes (`forge.security.permissions`): `SAFE` (read/analyze only), `ASSISTED` (modifications require approval; default), `AUTONOMOUS` (low-risk writes auto-approved; sensitive operations and high-risk writes still require approval), and `LOCKED` (no modifications). Denials are recorded and never bypassed; blocked operations can never escalate. Proposal (inspect/plan/model/tests) needs no write approval; every write and the commit pass the gate.
- **Structured coder schema** (`forge.agents.coder`): the coder accepts the legacy `{changes: {path: content}}` mapping and the richer `{summary, changes: [{path, action, content, risk, old_hash, old_content}], tests_to_run, reasoning_summary, risk_level, risks}` list schema, threads guards into the ChangeSet engine and policy gate, and surfaces the structured summary in its response metadata. Caller-supplied change shortcuts do not exist. The Model Fabric is the canonical production route; `router=` is a legacy compatibility adapter.
- **Bounded test/debug loop** (`forge.agents.debugger`): runs the relevant tests (or the full suite), captures command/exit code/stdout/stderr, and produces a structured `FailureReport` per failure with a recorded reason per retry. Repairs validate through the ChangeSet engine and authorize through the policy gate; a rejected repair fails honestly with no partial write. Retries are hard-bounded.
- **Independent review gate** (`forge.security.review`): a deterministic, severity-typed review (INFO/LOW/MEDIUM/HIGH/CRITICAL) producing `APPROVE`/`REQUEST_CHANGES`/`BLOCK`; HIGH/CRITICAL always block, and a configurable `ReviewPolicy` sets the tolerated `MEDIUM` budget (default: zero). An optional model-driven reviewer (`forge.agents.reviewer`) contributes findings through the Model Fabric (capability `review`), but the deterministic gate remains mandatory.
- **Real security verification** (`forge.security.verification`): secret/cloud/database/private-key patterns, dangerous execution (`eval`/`exec`, `os.system`/`os.popen`, `shell=True`, `sh`/`bash -c`), traversal, `.env`/credential/key files on sight, and unsafe or protected paths — with no hardcoded scores. Any finding fails the gate.
- **Acceptance engine** (`forge.core.acceptance`): aggregates the mandatory gates — tests, build, lint, review, security, benchmark, permissions, and rollback availability — into one `AcceptanceDecision` that names every failed gate (`failed_gates`) and carries measured `metrics`. One passing component can never override a failed mandatory gate, and unexecuted gates are never silently converted into success (an unconfigured lint checker passes only under the explicit, recorded pass-when-unconfigured policy).
- **Exact checkpoints** (`forge.tools.checkpoint`): exact original bytes plus restore metadata (`existed`, hash, size, mode) per path; rollback restores only candidate files, never uses `git reset --hard`, and preserves unrelated work.
- **Observability** (`forge.core.report`): every task produces a structured `TaskReport` (`task_id`, `trace_id`, stages, model/provider, `context_fingerprint`, files read/changed, commands run, tests run, gate results, checkpoint id, retries, duration, final status) plus an ordered `events` log, measured per-phase `timings`, and real model latency/token metadata — with secret redaction throughout, never raw prompts or credentials. `Supervisor.run()` returns the report under `result["report"]` alongside its existing keys.
- **Git safety**: commits stage only the explicitly validated touched files and never use `git add .`; staging rejects key material and credential files; `commit_accepted()` makes a commit impossible unless acceptance succeeded; failures restore the checkpoint exactly and preserve unrelated work.

See `docs/A32-HARDENED-LOOP.md` for the full A32 architecture, configuration examples, safety documentation, and the implemented/tested/optional/not-yet status matrix.

A deterministic provider that behaves like a model still exercises the *complete* orchestration path in the E2E tests; a separate opt-in real-model test drives Ollama end to end (see Testing).

## Permission & Policy Platform (A33)

A33 generalizes A32's policy core into a fine-grained platform controlling files, folders, tools, terminal commands, websites, browser actions, desktop resources, model providers, network access, and voice intents — with A32's `ALLOW` / `DENY` / `REQUIRE_APPROVAL` and modes still authoritative:

- **Policy engine** (`forge.security.policy`): `PermissionRequest` (who/what/where/when/how/risk/why) evaluated against `PermissionRule`s with deterministic most-specific-wins precedence (`DENY` → `REQUIRE_APPROVAL` → `ALLOW` → fail-closed default). Filesystem, terminal (exact-argv `ALLOW`s only), git, domain, network, model, desktop, and voice scopes; strict config validation; side-effect-free simulation; cached decisions with invalidation.
- **Approvals & task scope** (`forge.security.approvals`): structured approval requests, scoped single-use non-transferable time-bounded tokens (idempotent per enforcement chain, previewable without consumption), escalation requests that still need a distinct approver, and temporary task grants over declared files, revoked on failure/rollback/task end. Agents cannot self-grant.
- **Audit** (`forge.security.audit`): every decision recorded with request/task/trace ids, actor, resource, operation, scope, risk, decision, matched rules, and approval linkage — with secret redaction throughout.
- **Data classification** (`forge.security.classification`): `PUBLIC/INTERNAL/CONFIDENTIAL/SECRET` detection (detection always wins upwards) plus a model data policy enforced per candidate model by the Model Fabric, so classified content never reaches an unauthorized provider.
- **Tighten-only A32 integration**: the gate and runtime consult attached policies — explicit rules can restrict A32 verdicts further but never loosen them; with nothing attached, A32 behavior is byte-identical.
- **Safe foundations**: policy-gated mock browser/network/desktop (`forge.tools`), a voice interface routed through permissions (`forge.voice`), and cockpit backend interfaces for tasks/approvals/events (`forge.cockpit`). No real browser automation, sockets, input control, speech recognition, or frontend.

See `docs/A33-PERMISSION-PLATFORM.md` for the architecture, precedence algorithm, scope syntax, approval model, configuration format, threat model, and the ten tested security invariants.

## Browser Cockpit + Secure Control Plane (A34)

A34 operates Forge from the browser without trusting the browser. The
cockpit (`forge/cockpit/web/`, dependency-free, no build step) talks only
to the versioned API (`/api/v1/*`); the API talks only to the control
plane (`forge/control/`); the control plane drives the existing A32
supervisor through the existing A33 policy gates:

```bash
forge serve --project myproject=/path/to/repo
# open http://127.0.0.1:8000
```

- **Secure control plane**: project-scoped sessions, background dispatcher
  + worker over the existing task queue/supervisor, persistent replayable
  events (SSE live stream), approval adaptation over the A33 store
  (scoped/task-bound/time-bounded/non-replayable), pre-run checkpoints
  with candidate-scoped rollback through the policy gate, and JSONL audit.
- **Browser cockpit**: dashboard, tasks with a real pipeline timeline,
  WHAT/WHY approval cards, model fabric view, effective-permission view,
  git views, verification gates, and final reports.
- **Browser is never trusted**: server-side auth/scope/policy/approval for
  every call, CSRF + CORS lockdown, rate limits, no bypass fields, secret
  redaction, structured errors without tracebacks.
- **Core hooks are minimal and inert by default**: live events,
  cooperative pause/cancel, and interactive mid-run approval in the
  supervisor/ChangeSet path (`TaskCancelled` propagates; `DENY` is never
  escalated).

Auth is local-development sessions (no passwords). See
`docs/A34-BROWSER-COCKPIT.md` for the architecture, full API reference,
event catalog, security model, configuration, and honest limitations.

## Desktop Agent (A35)

A35 is controlled desktop execution. The agent (`forge/desktop/`) can
observe and act on a desktop, but every action — including observations —
passes the A33 permission system, deterministic risk classification, hard
security invariants, a permission profile, and (for actuation) the A33
approval store:

```
DesktopRequest → identity → task scope → A33 PolicyGate → risk/invariants
→ profile → approval (single-use token) → provider execution → audit
```

- **Structured vocabulary**: 15 action kinds with bounded validation as
  the first gate (target/keyboard/clipboard/args/coordinates limits,
  shell-metacharacter rejection, relative paths only).
- **Profiles**: SAFE (observe only), ASSISTED (actuation needs approval),
  AUTONOMOUS (LOW-risk actuation inside granted task scope), CUSTOM
  (tighten-only overrides). Hard invariants can never be overridden:
  credential extraction, security-software disabling, privilege
  escalation, persistence, remote control.
- **Approvals**: the same A33 store, single-use scope-bound tokens,
  distinct approver; stale/spent/out-of-scope tokens fail closed into a
  fresh request.
- **Provider protocol + fake desktop**: 16-method `DesktopProvider`
  protocol; deterministic scriptable `FakeDesktopProvider` (windows,
  processes, clipboard, input log, fault injection) for tests/dev. The
  cockpit labels the simulation as a simulation; A35 ships no real-OS
  provider — the protocol is the plugin point.
- **API + cockpit**: `/api/v1/desktop/capabilities|state|check|act|grants|
  approvals|approve|deny`, rate-limited, session-bound; a Desktop cockpit
  view with state, capability matrix, WHAT/WHY action form, and approval
  cards that carry the minted token into execution.

See `docs/A35-DESKTOP-AGENT.md` for the full architecture, security model,
and test matrix. Full suite after A35: 1022 passed, 2 skipped.

## Voice (A36)

A36 adds the audio layer around the A33 voice foundation — the complete
permission-gated spoken loop (`forge/voice/`):

```
audio (bounded WAV) → wake gate → transcription → VoiceCommand → intent
→ A33 policy + approval → action (task / spoken status) → spoken reply → audit
```

- **Deterministic simulated transport**: a text⇄tone codec over PCM —
  labeled simulation everywhere. The simulated recognizer refuses real
  audio instead of guessing; real STT/TTS/wake providers plug in behind
  the same protocols (`FORGE_VOICE_STT_PROVIDER` /
  `FORGE_VOICE_TTS_PROVIDER` accept only `simulated` in A36).
- **Voice can never bypass permissions**: commands execute with agent
  identity `forge-voice` through the A33 policy/approval system;
  approvals are session-bound with single-use tokens; unknown intents
  and un-woken audio fail closed.
- **API + cockpit**: `/api/v1/voice/capabilities|synthesize|transcribe|
  process|approvals|approve|deny`; a Voice cockpit view with the stack
  report, text commands, an audio round trip (synthesize → play → send
  through wake + recognition), full result traces, and playable spoken
  replies.

See `docs/A36-VOICE.md` for the architecture and honesty invariants.
Full suite after A36: 1075 passed, 2 skipped.

## Persistent Sessions + Memory (A37)

A37 makes the cockpit remember. On top of the already-durable cockpit
database (sessions, tokens, active-task bindings, run records), it adds:

- **Session memory** — SQLite-backed notes/facts/summaries that survive
  restarts, are strictly session-scoped, bounded (500 entries, 20 KB per
  entry, 1 MB per session, FIFO pruning), and served redacted at the API
  boundary.
- **Project memory** — durable project knowledge in the path-safe
  `MemoryStore` under `.forge/memory` (e.g. `facts/deploy`).
- **Run summaries** — bounded (last 50) outcome summaries recorded into
  project memory whenever a run finishes; disable with
  `ControlConfig(memory_record_runs=False)`.
- **Memory can never bypass permissions**: every access evaluates the
  A33 `Resource.MEMORY` policy (read/write/delete) with agent identity
  `forge-memory`; `REQUIRE_APPROVAL` files session-bound approvals that
  mint single-use tokens — stale or spent tokens fail closed into a
  fresh approval; read overviews are policy-filtered.
- **API + cockpit**: `/api/v1/memory*` (overview, session entries,
  project save/load/list, approvals) and a Memory cockpit view with
  notes, project keys, and approve/deny that carries the minted token
  into the resubmission.

See `docs/A37-PERSISTENT-SESSIONS-MEMORY.md` for the model, gating, and
test matrix. Full suite after A37: 1105 passed, 2 skipped.

## Multi-Agent Orchestration (A38)

A38 turns the agents into one coordinated team. A single requirement
becomes a deterministic, capability-matched plan that executes as a
dependency-ordered DAG: parallel where safe, sequential where
requested, with real budgets (bounded workers, per-step attempts and
timeouts) and cooperative cancellation.

- **The team is real**: planner, architect, researcher, coder, tester,
  debugger, reviewer, security, performance, documentation, and git
  executors each do their actual job over the project root. Planning
  never invents agents — unmatched requirements report `PLAN_REJECTED`.
- **Dispatch is permission-gated**: every step evaluates the A33
  `Resource.AGENT / execute` vocabulary with identity
  `forge-orchestrator`; `DENY` fails closed and `REQUIRE_APPROVAL`
  waits on the operator. Coder/debugger writes keep flowing through
  the existing ChangeSet + approval path.
- **Structured communication**: agent results travel as
  `AgentMessage` records with evidence and confidence — never
  uncontrolled free text for critical decisions.
- **API + cockpit**: `/api/v1/orchestrations*` (submit, list, get,
  cancel, per-orchestration approvals) and an Orchestrations cockpit
  view with live status, the plan, per-step outcomes, and
  approve/deny. Records are session-scoped and survive restarts.

See `docs/A38-ORCHESTRATION.md` for the engine, security model, and
test matrix. Full suite after A38: 1138 passed, 2 skipped.

## Vision & Multimodal Understanding (A39)

A39 adds provider-independent image understanding: bounded,
dependency-free parsing of PNG/JPEG/BMP/GIF (including a real PNG
chunk walker that surfaces embedded text), structured findings, and a
screenshot-to-action pipeline that proposes but never executes.

- **Vision input is untrusted**: images are bounded and treated as
  evidence, never authority. Text embedded in an image — even
  "approve everything" — is surfaced as a dangerous instruction and
  hard-blocked; it can never grant permissions.
- **Every analyze call is policy-gated** (`Resource.VISION /
  analyze`, single-use approval tokens); every proposed action is
  gated again (`Resource.VISION / execute`), and real execution
  stays on the browser/desktop bridges under their own gates.
- **Honest by construction**: the A39 simulated provider has no
  OCR/model and labels every result `simulation: true`; real
  providers plug in behind the same `VisionProvider` protocol.
- **API + cockpit**: `/api/v1/vision*` (capabilities, analyze,
  propose, approvals) and a Vision cockpit view with upload,
  understanding, proposals, and approve/deny.

See `docs/A39-VISION.md` for the security model, honesty invariants,
and test matrix. Full suite after A39: 1163 passed, 2 skipped.

## Computer Use (A40)

A40 combines vision with the desktop pipeline into controlled computer
use: `screen → understand → element tree → propose → PolicyGate →
execute` with versioned snapshots, redacted history, and hard guards.

- **No real actions under SAFE/LOCKED**; confirmation dialogs on
  screen fail closed; HIGH/CRITICAL-risk actions escalate to operator
  approval even under autonomous profiles; a per-task action budget
  caps executed actions.
- **Typed-text redaction**: history/logs/memory only ever see
  length-only placeholders — raw payloads reach only the provider.
- **Proposals never execute**: propose/cycle are dry runs; the
  simulated provider never changes the screen, so the loop honestly
  refuses to repeat actions.
- **API + cockpit**: `/api/v1/computer/*` (observe, propose, act,
  cycle, history, approvals) and a Computer cockpit view.

See `docs/A40-COMPUTER-USE.md` for the full security model and test
matrix. Full suite after A40: 1195 passed, 2 skipped.

## Premium Cockpit (A41)

The cockpit now covers the full surface — Overview, Tasks, Projects,
Models, Permissions, Approvals, Activity, Git, Desktop, Voice, Memory,
Orchestrations, Vision, Computer Use — plus three new live views:
**Agents** (documented inventory with each agent's real A33 gate and
honest simulation labels), **Security** (posture, hard invariants,
recorded evaluations — non-sensitive by construction), and
**Settings** (session profile, theme, shortcuts). Dark-first theme
with a complete light variant and a per-session toggle, responsive
layout under 760px, and the Ctrl+K command palette.

See `docs/A41-COCKPIT.md`. Full suite after A41: 1201 passed, 2
skipped.

## Natural Voice Conversation (A42)

Multi-turn voice on top of the A36 gate: deterministic context
resolution across turns, barge-in that blocks actions and recovers,
clarifying questions instead of guesses, confirm-before-execute (an
extra conversation-level gate — the A33 voice gate still applies),
and spoken results. Bounded (24 turns, 4 conversations per session),
session-isolated, and API-accessible at `/api/v1/voice/conversations*`.

See `docs/A42-VOICE-CONVERSATION.md`. Full suite after A42: 1216
passed, 2 skipped.

## Complete Supervisor transaction

`Supervisor.run(requirement, approved=True, router=...)` is the production integration point. It performs planning and capability selection before routing a model, then calls `CoderAgent` and always runs `TestDebugLoop`; it never skips directly to verification. A failing test supplies its captured output to `DebuggerAgent`, whose routed model response is applied and retested until success or the bounded retry limit. Only then do independent review, security, build/lint, benchmark, and acceptance run. Accepted files are explicitly staged and committed; every rejection restores the checkpoint and leaves unrelated working-tree files alone.

The executable supervisor E2E tests cover a deliberately broken first response followed by model repair, bounded rejection rollback, unrelated work preservation, explicit staging, `.forge` exclusion, and non-bypassable review/security rejection.

## Model Fabric

All model access is centralized in the Model Fabric (`forge.models`), the single infrastructure agents use to route and call models:

```
Agent → ModelFabric → FabricRouter → ModelRegistry → Provider → Model → ModelResponse → Telemetry → Router feedback
```

- **Model Registry** (`forge.models.registry`): declarative model entries with capabilities, context window, cost/free/local posture, and live health/reliability/latency. Derived `supports_*` accessors (tools, vision, code, reasoning, streaming, …) read from the declared capability tuple rather than hard-coding provider assumptions.
- **Provider Registry** (`forge.models.provider`): named provider adapters resolved by the router. `OllamaProvider` is first-class and credential-free; `OpenAIProvider` is an optional remote adapter, enabled only when a key is configured; `LocalModelProvider` is the deterministic offline fallback that refuses to invent source code. Proprietary support is never fabricated.
- **Capability-aware, context-aware, complexity-aware routing** (`forge.models.router.FabricRouter`): routes on a 19-capability vocabulary — coding, reasoning, planning, debugging, testing, review, security, research, documentation, vision, image_generation, audio, speech_to_text, text_to_speech, browser, computer_use, tool_use, structured_output, long_context — plus context size, task complexity, health, reliability, latency, and cost. Routing is deterministic (score → free → local → name).
- **Cost/free/local policy and deterministic fallback** (`forge.models.policy`): a strict policy is relaxed by a fixed fallback ladder (latency → reliability → remote → paid → health). Capability requirements are never relaxed: a vision request is never silently sent to a text-only model. Named presets (`quality`, `balanced`, `fast`, `free`, `local`, `privacy`) encode common postures; the `privacy` preset never relaxes the remote/paid constraints.
- **Structured `ModelRequest`/`ModelResponse`** (`forge.models.request`): provider-agnostic request/response types; failures return `success=False` with an error rather than raising. `ModelFabric.request()` is the canonical entry point.
- **Task/context/constraint propagation**: the fabric forwards `task`, repository `context`, and the rendered routing/generation `constraints` to the provider (introspecting each provider's signature so unsupported keywords are never passed). Production providers incorporate them into the model invocation: Ollama places the task in the native `system` slot and composes instructions/context/constraints into the prompt (with `num_predict`/`temperature` in `options`); OpenAI places the task as the system message and composes the rest into the user message (with `max_tokens`/`temperature`). No structured request information is silently dropped at the provider boundary, and secrets are never logged.
- **Capability verification levels** (`forge.models.registry`): each model capability is `declared`, `detected` (inferred by a conservative heuristic, e.g. Ollama vision by model family), or `verified` (confirmed by a real probe). Forge never assumes vision/audio/tool/structured-output support without evidence.
- **Honest multi-model consensus** (`forge.models.consensus`): deterministic aggregation (majority/unanimous/weighted/best) over real independent `ModelResponse`s. A single model response is never described as consensus.
- **Health tracking** (`forge.models.health`): success/failure/timeout counters, consecutive-failure circuit breaking, and bounded recheck after failure (no permanent blacklist).
- **Telemetry and router feedback** (`forge.models.telemetry`, `forge.models.feedback`): route/provider outcomes and feedback are recorded without persisting prompt/response content or credentials; an optional NDJSON sink can be enabled via configuration. Routing is documented as heuristic (weighted scoring), not machine learning.
- **Secure credential handling** (`forge.models.credentials`): credentials come from environment variables or a user-owned JSON file that Forge refuses to read unless it is owner-only (`0600`); secret values are never exposed in reprs, logs, or telemetry.
- **Errors** (`forge.models.errors`): a small `FabricError` hierarchy (`ModelUnavailableError`, `CapabilityNotSupportedError`, `ProviderError`, `ConfigurationError`) for branching on failure cause.
- **Model discovery** (`ModelFabric.discover_models()`): explicit, opt-in discovery for providers that expose it (Ollama `/api/tags`). Never downloads models automatically.
- **Configuration** (`forge.models.config`): environment variables or `.forge/models.yaml` / `.forge/models.json`.

### Streaming

`ModelFabric.stream()` has the same guarantees as `generate()`: capability/availability routing, policy filtering, the same failover chain, health/reliability/latency feedback, and telemetry. Chunks are buffered and only emitted after the provider completes, so a mid-stream failure never yields partial or duplicate output — the request fails over to the next candidate (or a single-chunk `generate()` fallback for providers without `stream`). When every candidate fails, `stream()` raises `ModelUnavailableError` rather than silently swallowing the failure. `OllamaProvider` streams natively (`stream: true`); providers without a streaming endpoint (OpenAI adapter, the local fallback, test doubles) degrade to a single complete response chunk.

`CoderAgent`, `DebuggerAgent`, `Supervisor.run`, and `SelfDevelopmentExecutor` all accept a `fabric=` argument and route through it; the legacy `router=` argument keeps working unchanged.

### Configuration

```yaml
# .forge/models.yaml (secrets go in environment variables, never here)
ollama_url: "http://127.0.0.1:11434"
ollama_model: "llama3.2"
ollama_enabled: true
default_capability: "coding"
default_model: null            # optional: always prefer this model
default_policy: "balanced"     # quality | balanced | fast | free | local | privacy
preferred_provider: null       # optional: prefer this provider name
local_only: false              # require local models (never remote)
free_only: false               # require free models (never paid)
max_retries: 3
timeout_seconds: 120
telemetry_enabled: true
telemetry_path: null           # optional NDJSON sink
policy:
  prefer_free: true
  prefer_local: true
  allow_remote: true
  allow_paid: true
```

## Providers

- `LocalModelProvider`: offline fallback with conservative no-op output when no local synthesis engine is configured.
- `OllamaProvider`: first-class local Ollama HTTP endpoint (no credentials required).
- `OpenAIProvider`: optional remote provider, enabled only when `OPENAI_API_KEY` is configured.
- `MockProvider`: test double only.

A provider can be registered with `ModelInfo(provider=...)` (legacy router) or `ModelFabric.register_model(...)` / `register_provider(...)` (fabric). Production callers should provide a real local or remote model for code generation; no pre-written `changes` are required by `CoderAgent`.

## Commands

```bash
forge plan "add CSV export"
forge analyze
forge models                   # list models (same as: forge models list)
forge models health            # model/provider health
forge models providers         # registered providers
forge models capabilities      # capability vocabulary
forge models test              # bounded local self-check
forge models --capability vision
forge models --json
forge self-analyze
forge self-improve --iterations 1
```

Writes, command execution, commits, pushes, repository deletion, and secret exposure remain permission-controlled. Forge is intentionally not an unattended deployment system.

## Testing

The suite includes repository intelligence and task lifecycle tests, checkpoint and permission coverage, model contract tests, verification gates, an isolated autonomous CSV-export E2E test, and a Model Fabric suite (capability vocabulary, registries, routing, fallback, health, telemetry, credentials, CLI, and agent/supervisor integration). Run:

```bash
python -m pytest -q
```

## Continuous integration

GitHub Actions (`.github/workflows/ci.yml`) runs on every push and pull request: it checks out the repository, installs the project with test dependencies (`pip install -e ".[dev]"`), runs `python -m pytest -q`, compiles the package (`python -m compileall forge`), and checks the diff (`git diff --check`). The job fails if any step fails; it never depends on a local Ollama server, so it is fully deterministic and offline. The opt-in live-model tests are skipped by default (see below).

Live Ollama integration tests are opt-in and auto-skip when no Ollama endpoint is reachable, so normal CI never fails merely because Ollama is not installed:

- `FORGE_LIVE_MODEL_TESTS=1` enables `tests/test_ollama_live.py` (live generation/telemetry smoke test).
- `FORGE_LIVE_OLLAMA=1` (or `FORGE_LIVE_MODEL_TESTS=1`) enables `tests/test_ollama_autonomous_e2e.py`, a genuine autonomous coding E2E: it creates an isolated repository, routes a real coding requirement through `ModelFabric` → Ollama, lets `CoderAgent` generate the change, writes it through the permissioned runtime, runs the tests, applies review/security verification, commits only the touched files, and verifies the resulting behavior. No model response is faked, and no caller-supplied `changes` or modifier function is used.
- `FORGE_TEST_OLLAMA_MODEL=<model>` targets a specific pulled model; otherwise the first model reported by `/api/tags` is used. `OLLAMA_BASE_URL`/`OLLAMA_URL` select the endpoint.

Status of live integration: **implemented and opt-in tested** where an Ollama endpoint is available; **not available** in environments without one (the suite remains fully runnable offline).

Known limitation: a useful autonomous run needs an available capable model provider (Ollama or an API provider); the dependency-free local fallback refuses to invent source code. This is a safe failure, not a deterministic fake implementation. Proprietary models are never fabricated: `OpenAIProvider` only works with a legitimate, operator-supplied `OPENAI_API_KEY`, and Forge makes no claim that any proprietary model is freely available.
