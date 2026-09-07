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
