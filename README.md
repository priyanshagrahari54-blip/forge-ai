# Forge AI

Forge is a repository-scoped software-engineering runtime. It combines repository intelligence, context selection, model routing, permissioned tools, bounded debugging, independent verification, checkpoints, and explicit Git staging.

## Execution architecture

1. `RepositoryIntelligence` indexes symbols, dependencies, architecture, runtime commands, and test mappings.
2. `AgentContextBuilder` selects relevant source, dependency, and test context under a token budget.
3. `ModelRouter` scores available providers using capability, complexity, context size, reliability, latency, cost/free status, and availability. Local/Ollama providers are first-class; paid APIs are optional.
4. `CoderAgent` asks the selected provider for a structured change (`changes: {path: content}`), validates it, and writes only through the permissioned runtime. A caller does not need to supply changes.
5. `TestDebugLoop` runs the repository test command, gives failures and context to a model, applies its bounded repair proposals, and reruns tests.
6. `VerificationPipeline` runs tests, compilation/build, lint/type-equivalent compilation, a secret scan, and an independent diff review. A failed gate prevents acceptance.
7. `CheckpointManager` snapshots the exact pre-change files and restores only files changed by the candidate; it does not use `git reset --hard` and leaves unrelated files alone.
8. `GitTool.stage_files` requires an explicit safe file list and rejects Forge runtime state. Autonomous commits never use `git add .`.

The self-development loop A26-A30 follows analyze → candidate → model-selected implementation → checkpoint → code → debug → verification → measurable benchmark → compare → commit or restore, and records history under `.forge/self/history/` (history is not committed).

## Complete Supervisor transaction

`Supervisor.run(requirement, approved=True, router=...)` is the production integration point. It performs planning and capability selection before routing a model, then calls `CoderAgent` and always runs `TestDebugLoop`; it never skips directly to verification. A failing test supplies its captured output to `DebuggerAgent`, whose routed model response is applied and retested until success or the bounded retry limit. Only then do independent review, security, build/lint, benchmark, and acceptance run. Accepted files are explicitly staged and committed; every rejection restores the checkpoint and leaves unrelated working-tree files alone.

The executable supervisor E2E tests cover a deliberately broken first response followed by model repair, bounded rejection rollback, unrelated work preservation, explicit staging, `.forge` exclusion, and non-bypassable review/security rejection.

## Providers

- `LocalModelProvider`: offline capability with conservative no-op output when no local synthesis engine is configured.
- `OllamaProvider`: optional local Ollama HTTP endpoint.
- `OpenAIProvider`: optional API provider, enabled only when `OPENAI_API_KEY` is configured.
- `MockProvider`: test double only.

A provider can be registered with `ModelInfo(provider=...)`. Production callers should provide a real local or remote model for code generation; no pre-written `changes` are required by `CoderAgent`.

## Commands

```bash
forge plan "add CSV export"
forge analyze
forge self-analyze
forge self-improve --iterations 1
```

Writes, command execution, commits, pushes, repository deletion, and secret exposure remain permission-controlled. Forge is intentionally not an unattended deployment system.

## Testing

The suite includes repository intelligence and task lifecycle tests, checkpoint and permission coverage, model contract tests, verification gates, and an isolated autonomous CSV-export E2E test. Run:

```bash
python -m pytest -q
```

Known limitation: a useful autonomous run needs an available capable model provider (Ollama or an API provider); the dependency-free local fallback refuses to invent source code. This is a safe failure, not a deterministic fake implementation.
