# Forge Agent Creation Engine (first-party)

Specs become validated, benchmarked, lifecycle-gated agent packages that
execute **only** through the mediated runtime: Model Fabric, PolicyGate,
Tool Runtime, namespaced Memory, Verification, Checkpoints.

## Spec: nine dimensions (`forge/agents/specs.py`)

`AgentSpec` — name, purpose, capabilities, tools, permissions,
model requirements, memory policy, verification requirements, resource
limits — plus `role`/`template` provenance. Validation is strict and
fail-closed:

- capabilities come from the canonical Model Fabric vocabulary;
- **tools** come from the executable set only: the seven real Tool
  Runtime tools (`read_file`, `write_file`, `delete_file`, `terminal`,
  `run_tests`, `search`, `git_status`) plus `memory_read`/`memory_write`,
  which the mediated runtime serves itself;
- **permissions** are requests validated against the real A33 engine
  vocabulary (`Resource` + `RESOURCE_OPERATIONS`); blank terminal scopes
  are rejected as unbounded shell grants;
- model requirements must be satisfiable by the agent's own capabilities;
- cross-agent memory reads can never be requested.

## Factory + lifecycle + versioning (`forge/agents/creation.py`)

`AgentCreationEngine` validates specs into structured `AgentPackage`
artifacts (spec snapshot, semver version, grants, version history,
benchmark evidence, transition trail). Persistence is a single atomic
JSON store; corrupt stores fail closed.

Lifecycle: `created → validated → tested → enabled ⇄ paused`, with
`disabled` and terminal `retired`. Only `enabled` agents run.

Power changes reset trust: spec updates, grants, and revocations bump a
patch version and return the package to `created`, so broader power must
re-earn validation, testing, and enablement. Release-only version bumps
(`publish_version`) record a spec hash and keep state.

## No self-grants, no self-administration

Permission requests are inert until an operator (never the agent itself)
grants them by index via `grant_permission`. Creation, validation,
benchmarking, transitions, updates, and versioning all refuse agent
identities as actors. Exports carry spec + versions only — grants never
travel, so imports arrive ungranted. Grant-shaped tool calls
(`*grant*`, `*approve*`, …) are refused at execution time.

## Mediated execution (`forge/agents/mediation.py`)

`GatedAgentRuntime` is the single choke point. Every run: lifecycle gate
→ self-approval refusal → governor quotas → Model Fabric routing (spec
bounds mapped onto the real `ModelRequest`) → allowlisted tool calls
(mutating tools pre-authorized by the real PolicyGate, checkpoint taken
before the first mutation, rollback on tool/verification failure) →
verification (real secret/dangerous patterns, real review gate, real test
command for `require_tests`) → namespaced memory write. Refusals carry
stable machine codes (`NOT_ENABLED`, `TOOL_DENIED`, `GATE_DENIED`,
`ISOLATION`, `RATE_LIMITED`, …).

## Benchmarks (`forge/agents/agent_bench.py`)

Ten deterministic code-judged checks: stored-spec re-validation (spec,
tools, permissions, memory, verification, limits), lifecycle gate,
memory isolation, permission boundary, and a model smoke test (skipped
— never passed — without a fabric). The score must clear the spec's
`min_benchmark_score` before `validated → tested`.

## Templates

Six first-party templates: `coding`, `research`, `security`,
`game-dev`, `os-dev`, `documentation` — least-privilege tools and
permissions per role, all re-validated on build (overrides included).

## Surfaces

- **Control plane**: `engine_*` methods (audited) — templates, create,
  get/list, update, validate, test, enable/pause/resume/disable/retire,
  grant/revoke, version, export/import, run (AGENT/execute pre-gate with
  approval filing + mediated runtime with real tools).
- **API**: `/api/v1/engine/*` (`forge/api/routes_engine.py`).
- **CLI**: `forge agents list|templates|create|show|validate|test|enable|
  pause|resume|disable|retire|grant|revoke|update|version|run`
  (`--store`/`--actor`/`--json` accepted before or after the subcommand).
- **Desktop**: Agents menu → Agent Manager window over
  `DesktopBackend.agents_*`.

## Testing

`tests/test_creation_engine.py` (specs, templates, factory, lifecycle,
updates, versioning, grants, export/import, persistence) and
`tests/test_creation_mediation.py` (mediation against real and fake
subsystems, benchmarks, isolation + permission boundaries, control
plane, API, CLI, desktop backend + window) — 91 tests.
