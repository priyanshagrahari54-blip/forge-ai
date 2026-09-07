# Forge AI final hardening progress

## A31 — Model Fabric (centralized model infrastructure)

- Built the Model Fabric under `forge/models/` as the single path from agent to model: `Agent → ModelFabric → FabricRouter → ModelRegistry → Provider → Model → ModelResponse → Telemetry → Router feedback`.
- Canonical 19-capability vocabulary (`capabilities.py`): coding, reasoning, planning, debugging, testing, review, security, research, documentation, vision, image_generation, audio, speech_to_text, text_to_speech, browser, computer_use, tool_use, structured_output, long_context. Capability requirements are never relaxed during fallback.
- Model Registry (`registry.py`) and Provider Registry (`provider.py`) with name-keyed registration, capability lookup, availability, snapshot, per-model health/reliability/latency state, and declarative `supports_*` accessors derived from capabilities (no hard-coded provider assumptions).
- Capability-, context-, and complexity-aware routing (`router.py:FabricRouter`) with a deterministic cost/free/local policy (`policy.py`) and a fixed fallback ladder (latency → reliability → remote → paid → health). Fallback models (the local no-op) only serve when no regular model can. Named policy presets (`quality`/`balanced`/`fast`/`free`/`local`/`privacy`); the `privacy` preset never relaxes the remote/paid posture. `default_model` and `preferred_provider` preferences reorder the candidate chain without bypassing filtering.
- Health tracking (`health.py`) with degrade/unhealthy/recover thresholds, timeout tracking, last-success/last-failure timestamps, and bounded recheck after failure (no permanent blacklist). Reliability (EMA) and latency (EMA) tracking; router feedback (`feedback.py`) applies outcomes back to future routing (heuristic weighted scoring, not machine learning).
- Structured `ModelRequest`/`ModelResponse` (`request.py`); provider failures return `success=False` responses with deterministic failover down the candidate chain instead of raising. `ModelFabric.request()`/`select()`/`available_models()`/`models_for_capability()`/`record_result()`/`stream()`/`discover_models()`/`provider_health()` form the public API.
- Ollama first-class (`OllamaProvider`), including conservative vision-model detection, `list_models`, streaming, configurable timeout, and a live health probe; `OpenAIProvider` remains an optional remote adapter enabled only with a configured key. Provider adapters are plain objects implementing `generate()` (a Protocol), so future providers (local custom, additional remotes) register without touching agents/supervisor. No proprietary support is fabricated.
- Telemetry (`telemetry.py`) records route/response/error/feedback events without persisting prompt/response content or credentials; optional NDJSON file sink via configuration.
- Secure credentials (`credentials.py`): environment variables or a user-owned JSON file that is refused unless owner-only (0600); secret values never appear in reprs, logs, or telemetry.
- Errors (`errors.py`): `FabricError` hierarchy (`ModelUnavailableError`, `CapabilityNotSupportedError`, `ProviderError`, `ConfigurationError`).
- Configuration (`config.py`): environment variables layered over `.forge/models.yaml`/`.forge/models.json`; supports `default_model`, `default_policy`, `preferred_provider`, `local_only`, `free_only`, `max_retries`, `timeout_seconds`, and telemetry settings. Secrets are never stored in config.
- Agent integration: `CoderAgent`, `DebuggerAgent`, `Supervisor.run`, and `SelfDevelopmentExecutor` accept `fabric=` and route through the fabric; the legacy `router=` path is preserved verbatim. Supervisor run reports now record the selected model/provider plus fabric routing history.
- CLI: `forge models [list|health|providers|capabilities|test]`, plus `--capability`, `--capabilities`, `--json`.
- Legacy `ModelInfo`/`ModelRouter`/providers are fully backward compatible; `ModelFabric.legacy_router()` exposes a legacy view.

## Executable proof (A31)

- `tests/test_model_fabric_*.py`: capability vocabulary, model/provider registries, policy (+ presets, privacy non-relaxation), telemetry, credentials, errors, FabricRouter (capability/context/complexity/cost-free-local/health/fallback), ModelFabric (generate, deterministic failover, feedback, snapshot, no-prompt-in-telemetry, request/select/stream/discover/provider-health/preferred-provider/default-model/privacy), CLI subcommands, and agent/self-development wiring.
- `tests/test_model_fabric_supervisor_e2e.py`: full Supervisor transaction (plan → model code → failing test → fabric-routed repair → retest → review/security → commit) with every model call routed and recorded by the fabric.
- `tests/test_ollama_live.py`: optional live Ollama integration, auto-skipped when no endpoint is reachable.

## Implemented and executable

- `Supervisor.run()` executes planning, capability/agent selection, model routing, model-generated code, permissioned writes, real tests, bounded `TestDebugLoop` repair/retest, independent review, security, build, configured lint/type checks, benchmark, acceptance, explicit validated-file commit, or checkpoint rollback.
- The production Supervisor API accepts no caller-supplied changes and no modifier function. The legacy self-development `modifier_fn` remains only as a documented compatibility hook for pre-existing unit tests; autonomous self-development omits it and uses the same CoderAgent/DebuggerAgent/VerificationPipeline infrastructure.
- Ollama is a first-class free/local provider. Its unavailable-model error is surfaced and recorded; the safe local fallback refuses arbitrary synthesis rather than claiming success. OpenAI remains optional.
- Model responses are validated as structured JSON with an explanation, mapping of UTF-8 string contents, repository-relative safe paths, size limits, and secret rejection before any write. Debug repairs reuse the same validator.
- Test failure output includes stdout/stderr. Each repair attempt records attempt number, failure, diagnosis, modified files, model, latency, and test result. Retry count has a hard bound.
- Checkpoints restore candidate files exactly, delete declared newly created candidate files, preserve unrelated modified/untracked files and `.forge` state, and never use hard reset/clean.
- Git verifies the staged set equals the validated candidate set and rejects `.git`, `.forge`, environment, credential, secret, and private-key paths. No autonomous path uses broad staging.
- Security checks cover secret/cloud/database/private-key patterns, dynamic execution, shell execution, and traversal. Review checks changed material for conflict markers, incomplete implementations, dynamic execution, and suspicious test weakening. Failed gates block acceptance.
- Build uses `compileall`; configured Ruff and mypy checks are executed when declared by project configuration, otherwise the omission is recorded rather than fabricated.
- Benchmarks record task/test/repair success, retries, test/build/benchmark latency, model latency, files changed, and rollback count. Self-development requires measurable candidate improvement unless using the clearly documented legacy modifier hook.
- Candidate IDs use stable hashes of finding category, target files, and proposed change rather than sequential indices. Router feedback records model, capability, success/failure, latency, and task complexity and updates future scoring.
- Structured run observability includes run ID, requirement/task, selected agents/model, context fingerprint, stages, attempts, files, gates, benchmark, acceptance, rollback, failure reason, and duration.

## Executable proof

- `tests/test_supervisor_autonomous_e2e.py`: isolated repository; deliberately incorrect first model response; real pytest failure; failure passed to second model call; repair; passing retest; validated-only commit; `.forge` exclusion; unrelated work preservation.
- `tests/test_supervisor_safety_e2e.py`: bounded rejection rollback, commit failure rollback, routing failure history/latency, and no commit after failure.
- `tests/test_verification_rejection_e2e.py`: tests pass but security or review fails; both reject and roll back without commit.
- `tests/test_model_contract.py`: malformed JSON, unsafe paths, secrets, unavailable provider, and no-write behavior.
- `tests/test_self_autonomous_e2e.py`: isolated self-development analysis → candidate → model modification → test/verification/benchmark → explicit commit.

## Deliberate limitations

- This repository includes deterministic provider-contract tests, not a claim that a real model was available during CI. Real autonomous coding requires an Ollama model (or optional API provider) configured by the operator.
- Approval and permission gates remain enabled; Forge does not silently push or access unauthorized repositories.

## A01–A31 audit & hardening (this pass)

- Audited every stage in the A01–A31 pipeline against the actual code; see
  `docs/A01-A31-AUDIT.md` for the full matrix, baseline/final numbers, and
  limitations. Statuses are evidence-derived, never taken from this file.
- Fixed (with regression tests in `tests/test_audit_hardening.py`):
  - Debug loop now records the successful final retest and runs tests with
    `-B -p no:cacheprovider` (no stale-bytecode false results).
  - Memory store hardened: path confinement + size bound + lifecycle.
  - Self-analyzer no longer fabricates passed-test or permission metrics.
  - Coder validates generated Python with `compile()` before writing.
  - Security gate flags `.env` files on sight and skips vendored/cache dirs.
  - Checkpoint manager and candidate evaluator skip vendored/cache dirs.
  - Terminal tool caps captured output at 200 KB.
  - Model registry records per-capability verification level
    (declared/detected/verified); Ollama vision is `detected`.
  - New deterministic multi-model consensus (`forge/models/consensus.py`).
  - Planner validates empty input and orders steps by dependency.
- Test count: 365 → 382 passed (1 skipped: opt-in live Ollama).
- Live-provider verification was not run (no Ollama endpoint in this sandbox).

## A31 hardening pass — provider propagation + streaming + live E2E

- **Task/context/constraint propagation**: `ModelRequest.constraints_text()` renders routing/generation constraints; `compose_provider_prompt()` builds the labeled TASK/INSTRUCTIONS/REPOSITORY CONTEXT/CONSTRAINTS input. `ModelFabric.generate()`/`stream()` forward `task`, `context`, `instructions`, `max_output_tokens`, and `temperature` to providers via signature introspection (unsupported keywords are never passed). Ollama uses the native `system` slot + `options.num_predict`/`temperature`; OpenAI uses native `messages` (system=task, user=composed) + `max_tokens`/`temperature`; the local fallback accepts the full request but still refuses to fabricate code.
- **Streaming reliability**: `ModelFabric.stream()` now mirrors `generate()` — same routing/capability/availability/policy, same failover chain, buffered chunks (no partial/duplicate output on failure), single-chunk `generate()` fallback for non-streaming providers, health/reliability/latency feedback, telemetry, and a raised `ModelUnavailableError` when all candidates fail.
- **Ollama URL normalization**: bare-host `OLLAMA_BASE_URL`/`OLLAMA_URL` values are normalized to `/api/generate` (previously a bare host POSTed to the server root).
- **Live autonomous E2E**: `tests/test_ollama_autonomous_e2e.py`, opt-in via `FORGE_LIVE_OLLAMA=1` (or `FORGE_LIVE_MODEL_TESTS=1`), drives the full Supervisor→Fabric→Ollama→coder→permissioned-write→tests→review/security→commit path and verifies real repository behavior. Skipped cleanly without Ollama.
- Tests added: `test_model_fabric_provider_payload.py` (11), `test_model_fabric_streaming.py` (6), `test_ollama_autonomous_e2e.py` (1 opt-in). Total: 382 → 399 passed, 2 skipped.

## A32 — Autonomous Engineering Core

- **Controlled code-change application** (`forge/tools/change_applier.py`): `CodeChange`/`ApplyResult`/`ChangeApplier` validate paths (relative, no `..`/`.git`/`.forge`/backslashes), reject secrets/credentials/`.env`/oversized/invalid-Python content, write through the permissioned `ToolRuntime`, record changed paths, and checkpoint before the first write (with exact rollback). `CoderAgent` writes through this layer.
- **Structured coder schema**: `CoderAgent._parse_changes` accepts both the legacy `{changes: {path: content}}` mapping and the richer `{summary, changes: [{path, action, content}], tests, reasoning_summary, risks}` list schema; `summary`/`reasoning_summary`/`risks`/`tests` are surfaced in response metadata. Deletions are rejected.
- **Context completeness**: `AgentContextBuilder` seeds a deterministic repository-fallback context when the relevance engine selects nothing, so the model never receives an empty context.
- **Debug recovery loop**: `DebugAttempt` now records `command` and `exit_code`; `_repair_prompt` includes actual diagnostics plus `PREVIOUS ATTEMPTS` so the model changes strategy; retries remain hard-bounded.
- **Review gate** (`forge/security/review.py`): deterministic `ReviewDecision` (APPROVE/REQUEST_CHANGES/BLOCK) with severity (INFO…CRITICAL); HIGH/CRITICAL block. Optional model-driven `ReviewerAgent` (capability `review`) merges findings; the deterministic gate always runs.
- **Acceptance engine** (`forge/core/acceptance.py`): central `AcceptanceDecision` aggregating tests/build/lint/review/security/benchmark/permissions/rollback; a failed mandatory gate always rejects.
- **Permission modes** (`forge.security.permissions.py`): `OperationMode` SAFE/ASSISTED/AUTONOMOUS/LOCKED wired into `ToolRuntime` via `PermissionManager.may_execute` (backward compatible; ASSISTED preserves existing behavior). Modes only restrict, never escalate.
- **Observability** (`forge/core/report.py`): structured `TaskReport`; `Supervisor.run()` returns `result["report"]`, `result["review"]`, `result["acceptance"]`, `result["checkpoint_id"]`.
- **Supervisor integration**: runs the full lifecycle (plan → agents → model → controlled code → test/debug → review → security → benchmark → acceptance → checkpoint → explicit commit/rollback) and reports it.
- Tests: `test_a32_change_applier.py` (15), `test_a32_review.py` (11), `test_a32_acceptance.py` (8), `test_a32_permissions_mode.py` (7), `test_a32_coder_schema.py` (6), `test_a32_e2e.py` (3) — 50 new tests. Total: 399 → 449 passed, 2 skipped (opt-in live Ollama).

Known limitations: autonomous deletions are rejected (operator must delete explicitly); the model-driven reviewer is optional and only runs when a review-capable model is available through the fabric; live Ollama E2E remains opt-in.

## A31 final hardening — CI + merge readiness

- **CI added**: `.github/workflows/ci.yml` runs on every push and pull request — checkout, `pip install -e ".[dev]"`, `python -m pytest -q`, `python -m compileall forge`, `git diff --check`. The job fails on any failing step and never depends on a local Ollama server (deterministic/offline).
- **Generated artifacts removed from Git tracking**: `forge_ai.egg-info/*` (PKG-INFO, SOURCES.txt, dependency_links.txt, entry_points.txt, requires.txt, top_level.txt) was accidentally tracked; removed from the index. It remains on disk only as an ignored, regenerable build artifact.
- **Real test numbers**: `python -m pytest -q` → **449 passed, 2 skipped** (2 skips = opt-in live Ollama tests, no endpoint in the run environment). `python -m compileall forge` clean; `git diff --check` clean. PR #4 description updated from the stale "365 passed, 1 skipped" to the recorded value.
- **Merge state**: branch `arena/01a0777f-forge-ai` is up to date with `main` (it is ahead, not behind); PR #4 reports `mergeable=TRUE`, `mergeable_state=CLEAN`.
- **Verification without fabrication**: no hardcoded `return True`/`return 0` success paths in the security/self-development gates; provider propagation, streaming parity/failover, and opt-in Ollama E2E are each backed by executable tests (see `tests/test_model_fabric_*.py`, `tests/test_ollama_*.py`).

## A31 — CI push status (blocked on App permission)

- The CI workflow `.github/workflows/ci.yml` is written, validated, and committed on the PR branch at `38f7429` (checkout → `pip install -e ".[dev]"` → `python -m pytest -q` → `python -m compileall forge` → `git diff --check`).
- It is **not yet on the GitHub remote**: `git push` is rejected with `refusing to allow a GitHub App to create or update workflow '.github/workflows/ci.yml' without 'workflows' permission`, and the Contents API returns 403 `Resource not accessible by integration`. The `arena-ai-coding-agent[bot]` App installation lacks the **Workflows (read and write)** permission.
- Consequence: GitHub Actions cannot run against the PR head until the App is granted `workflows: write` (or GitHub is reconnected in Arena with a token that has it). No CI run is claimed; the local suite (449 passed, 2 skipped) is the verification evidence in the meantime.
- Exact next action: repo owner grants the App **Workflows → Read and write** on this repository (Settings → GitHub Apps → installed app → permissions), then `git push origin arena/01a0777f-forge-ai` delivers `38f7429` and CI runs.

## A32 rebuild — production-hardened autonomous loop

The previous A32 commits were no longer reachable, so A32 was rebuilt cleanly
on `arena/01a07aa1-forge-ai` from the A31 tree (baseline: 449 passed,
2 skipped). Each milestone below is a separate commit; no reset, rebase, or
squash was used. Final: **550 passed, 2 skipped** (skips = opt-in live
Ollama), `compileall` clean, `git diff --check` clean.

- **Controlled ChangeSet engine** (`3a25a0a`): deterministic fingerprints,
  `dry_run()` with zero writes, old-content/hash guards, structured
  `ChangeError` values, and a `delete` action gated behind `allow_delete`
  plus explicit approval (model-proposed deletes stay rejected).
  Tests: `tests/test_a32_changeset.py` (16).
- **Permission policy gate** (`1c4e78b`): explicit
  `ALLOW`/`DENY`/`REQUIRE_APPROVAL` over operation, path, tool, risk, and
  requested capability; `SAFE`/`ASSISTED`/`AUTONOMOUS`/`LOCKED` modes with
  risk-aware autonomous auto-approval. The engine authorizes every change
  before modification; denials are recorded, never bypassed.
  Tests: `tests/test_a32_policy_gate.py` (20).
- **Model-driven coding pipeline** (`3e48af1`): coder schema gains
  `tests_to_run`/`risk_level` plus per-change `risk`/`old_hash`/`old_content`
  guards threaded into the ChangeSet engine and policy gate; the legacy
  `router=` path is preserved and `request.metadata["changes"]` shortcuts
  are behaviorally proven absent.
  Tests: `tests/test_a32_coding_pipeline.py` (11).
- **Test/debug/repair loop** (`6f72f46`): repairs validate through the
  ChangeSet engine and authorize via the policy gate; structured
  `FailureReport`s, recorded retry reasons, and targeted runs from the
  coder's `tests_to_run` (the acceptance gate still runs the full suite).
  Tests: `tests/test_a32_debug_loop.py` (9).
- **Independent review and security gates** (`8c346c4`): configurable
  `ReviewPolicy` `MEDIUM` budget with `HIGH`/`CRITICAL` always blocking;
  security flags key/credential files on sight, unsafe and protected paths,
  and shell/`popen` command usage — no hardcoded scores.
  Tests: `tests/test_a32_gates.py` (12).
- **Acceptance and rollback verification** (`c5b43d8`): decisions name
  `failed_gates` and carry measured `metrics` with explicit lint-execution
  evidence; checkpoints record exact restore metadata; staging rejects key
  material; `commit_accepted()` refuses commits unless acceptance succeeded.
  Tests: `tests/test_a32_acceptance_safety.py` (15).
- **Autonomous engineering E2E** (`632f3c2`): fabric-path failure matrix —
  malformed output, unauthorized/traversal paths, secrets, credential files,
  repair failure, review/security rejection, commit failure — each proving
  rejection, exact rollback, no commit, and structured reporting.
  Tests: `tests/test_a32_failure_matrix.py` (9).
- **Supervisor observability and timing** (`b8b4af2`): ordered redacted
  event log, measured per-phase timings, real model latency/token metadata
  (`None` when unreported, never fabricated).
  Tests: `tests/test_a32_observability.py` (9).
- **Documentation** (this entry + `docs/A32-HARDENED-LOOP.md` + README):
  architecture, configuration examples, safety/permission docs, and an
  implemented/tested/optional/not-yet matrix.

Known limitations: model-proposed deletes are rejected by design (explicit
operator path only); the model reviewer needs a review-capable model;
lint/type checks run only when the target declares them (omission recorded);
live Ollama E2E remains opt-in and was not run here (no endpoint).
