# Forge Agent Creation Engine (first-party)

Specs become validated, benchmarked, lifecycle-gated agent packages that
execute **only** through the mediated runtime: Model Fabric, PolicyGate,
Tool Runtime, namespaced Memory, Verification, Checkpoints.

## Spec: nine dimensions (`forge/agents/specs.py`)

`AgentSpec` — name, purpose, capabilities, tools, permissions,
model requirements, memory policy, verification requirements, resource
limits — plus `role`/`template` provenance. Serialized specs carry a
`spec_version` envelope (`"1.0"`); unknown versions fail closed at parse
time. Validation is strict and fail-closed, and parsing refuses silent
coercion (no `int()`/`float()`/`str()` guessing, no bools-as-numbers):

- capabilities come from the canonical Model Fabric vocabulary;
- **tools** come from the executable set only: the seven real Tool
  Runtime tools (`read_file`, `write_file`, `delete_file`, `terminal`,
  `run_tests`, `search`, `git_status`) plus `memory_read`/`memory_write`,
  which the mediated runtime serves itself;
- **permissions** are requests validated against the real A33 engine
  vocabulary (`Resource` + `RESOURCE_OPERATIONS`); blank or
  wildcard-only terminal scopes are rejected as unbounded shell grants,
  and filesystem scopes must be valid repo-relative patterns;
- `require_tests` requires the `run_tests` tool (the only test runner);
- model requirements must be satisfiable by the agent's own capabilities;
- cross-agent memory reads can never be requested.

## Factory + lifecycle + versioning (`forge/agents/creation.py`)

`AgentCreationEngine` validates specs into structured `AgentPackage`
artifacts (spec snapshot, semver version, grants, version history,
benchmark evidence, transition trail). Persistence is a single atomic
JSON store (bounded size and package count, optimistic mtime guard
against concurrent writers, fsync before replace); corrupt stores fail
closed, and stored grants re-validate on load.

Lifecycle: `created → validated → tested → enabled ⇄ paused`, with
`disabled` and terminal `retired`. Only `enabled` agents run, and
enablement (from any state, including `resume`) requires the latest
benchmark to pass **against the current spec hash** — stale results
never re-enable; re-running the benchmark self-heals.

Power changes reset trust: spec updates, grants, and revocations bump a
patch version and return the package to `created`, so broader power must
re-earn validation, testing, and enablement. Release-only version bumps
(`publish_version`) record a full SHA-256 spec hash and keep state.

## No self-grants, no self-administration, no anonymous operators

Permission requests are inert until a **named** operator (never the agent
itself, in any spelling or case) grants them by index via
`grant_permission`, optionally pinning the reviewed entry (`expected`)
so a spec change between listing and granting refuses instead of
granting a re-pointed index. Creation, validation, benchmarking,
transitions, updates, versioning, and runs all refuse agent identities
and empty actors. Exports carry spec + versions only — grants never
travel, so imports arrive ungranted. Grant-shaped tool calls
(`*grant*`, `*approve*`, …) are refused at execution time.

## Grants are enforced (`forge/agents/mediation.py`)

The allowlist says what an agent may *call*; grants say what the
operator *approved*. Every tool call needs a recorded grant covering
its target scope, with approval-store semantics: filesystem scopes
match as repo-relative patterns, terminal scopes pin the exact argv[0]
(`run_tests` always runs pytest, so it needs `terminal`/`execute`
pinned to `"pytest"`), search needs a broad filesystem/read grant,
and git/memory use exact scope with blank covering any target. The
matching grant's risk feeds the PolicyGate; only an explicit `ALLOW`
proceeds for mutating tools. Approval tokens redeem once per call
across the gate and runtime layers via a shared redemption chain.

## Mediated execution (`forge/agents/mediation.py`)

`GatedAgentRuntime` is the single choke point. Every run: lifecycle gate
→ named-actor + self-approval refusal → governor quotas → Model Fabric
routing (spec bounds mapped onto the real `ModelRequest`) → grant-checked,
allowlisted tool calls (mutating tools pre-authorized by the real
PolicyGate, checkpoint taken before the first mutation — mutating runs
without a checkpoint manager are refused — rollback on tool/verification
failure) → verification (real secret/dangerous patterns reported by
index, never echoed; real review gate; the harness's **fixed** project
pytest suite for `require_tests` — callers supply no command, so there
is nothing to inject) → namespaced memory write. Refusals carry stable
machine codes (`NOT_ENABLED`, `TOOL_DENIED`, `GATE_DENIED`,
`ISOLATION`, `RATE_LIMITED`, …). Run reports record actor, version, and
spec hash evidence.

Trust boundary: `approved=True` means trusted-local-operator consent
(the same flag the gate and tool runtime honor). Remote API callers can
never set it — the API offers no such field — and API tool approval
flows through redeemable approval tokens only.

## Benchmarks (`forge/agents/agent_bench.py`)

Eleven deterministic code-judged checks: stored-spec re-validation
(spec, tools, permissions, memory, verification, limits), lifecycle
gate, memory isolation, permission boundary, grant enforcement (an
ungranted power tool must refuse), and a model smoke test (skipped —
never passed — without a fabric). The boundary checks are mandatory:
the verdict requires all of them green no matter how low the spec's
`min_benchmark_score` goes. Reports carry the benchmarked spec's hash
so enablement binds results to the current spec.

## Templates

Six first-party templates: `coding`, `research`, `security`,
`game-dev`, `os-dev`, `documentation` — least-privilege tools and
permissions per role, all re-validated on build (overrides included),
and every template tool is grantable from the template's own requests.

## Surfaces

- **Control plane**: `engine_*` methods (audited) — templates, create,
  get/list, update, validate, test, enable/pause/resume/disable/retire,
  grant/revoke (with `expected` pin), version, export/import, run
  (AGENT/execute pre-gate with approval filing + mediated runtime with
  real tools). Remote payloads are size-bounded (64KB specs, 8KB tool
  args, ≤25 calls); there is no caller approval flag and no caller
  test command on the run path.
- **API**: `/api/v1/engine/*` (`forge/api/routes_engine.py`).
- **CLI**: `forge agents list|templates|create|show|validate|test|enable|
  pause|resume|disable|retire|grant|revoke|update|version|run`
  (`--store`/`--actor`/`--json` accepted before or after the subcommand;
  `show` prints indexed permissions/grants, `grant` accepts
  `--expect-json`, `run` attaches the real tool runtime for the fixed
  pytest gate).
- **Desktop**: Agents menu → Agent Manager window over
  `DesktopBackend.agents_*`.

## Testing

`tests/test_creation_engine.py` (specs, templates, factory, lifecycle,
updates, versioning, grants, export/import, persistence) and
`tests/test_creation_mediation.py` (mediation against real and fake
subsystems, benchmarks, isolation + permission + grant boundaries,
control plane, API, CLI, desktop backend + window).
