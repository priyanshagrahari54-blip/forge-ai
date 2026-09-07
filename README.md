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
- **Structured `ModelRequest`/`ModelResponse`** (`forge.models.request`): provider-agnostic request/response types; failures return `success=False` with an error rather than raising. `ModelFabric.request()` is the canonical entry point; `stream()` is available for providers that support it and degrades to a single-chunk yield otherwise.
- **Capability verification levels** (`forge.models.registry`): each model capability is `declared`, `detected` (inferred by a conservative heuristic, e.g. Ollama vision by model family), or `verified` (confirmed by a real probe). Forge never assumes vision/audio/tool/structured-output support without evidence.
- **Honest multi-model consensus** (`forge.models.consensus`): deterministic aggregation (majority/unanimous/weighted/best) over real independent `ModelResponse`s. A single model response is never described as consensus.
- **Health tracking** (`forge.models.health`): success/failure/timeout counters, consecutive-failure circuit breaking, and bounded recheck after failure (no permanent blacklist).
- **Telemetry and router feedback** (`forge.models.telemetry`, `forge.models.feedback`): route/provider outcomes and feedback are recorded without persisting prompt/response content or credentials; an optional NDJSON sink can be enabled via configuration. Routing is documented as heuristic (weighted scoring), not machine learning.
- **Secure credential handling** (`forge.models.credentials`): credentials come from environment variables or a user-owned JSON file that Forge refuses to read unless it is owner-only (`0600`); secret values are never exposed in reprs, logs, or telemetry.
- **Errors** (`forge.models.errors`): a small `FabricError` hierarchy (`ModelUnavailableError`, `CapabilityNotSupportedError`, `ProviderError`, `ConfigurationError`) for branching on failure cause.
- **Model discovery** (`ModelFabric.discover_models()`): explicit, opt-in discovery for providers that expose it (Ollama `/api/tags`). Never downloads models automatically.
- **Configuration** (`forge.models.config`): environment variables or `.forge/models.yaml` / `.forge/models.json`.

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

A live Ollama integration test (`tests/test_ollama_live.py`) is opt-in: set `FORGE_LIVE_MODEL_TESTS=1` (and, optionally, `FORGE_TEST_OLLAMA_MODEL` to target a specific pulled model). It auto-skips when no Ollama endpoint is reachable, so normal CI never fails merely because Ollama is not installed.

Known limitation: a useful autonomous run needs an available capable model provider (Ollama or an API provider); the dependency-free local fallback refuses to invent source code. This is a safe failure, not a deterministic fake implementation.
