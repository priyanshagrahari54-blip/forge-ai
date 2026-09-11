# Forge Agent Creation Engine (first-party)

Forge can create new specialized software agents from structured
specifications. The engine is the single first-party path for doing so:
specs become validated, benchmarked, lifecycle-gated packages, and those
packages execute only through the mediated runtime.

## Specification (9 dimensions)

`forge/agents/specs.py` — `AgentSpec`:

| Field | Meaning |
|---|---|
| `name` | `[a-z][a-z0-9_-]{2,48}`, lowercased |
| `purpose` | 1–500 chars, what the agent is for |
| `capabilities` | 1–12 entries from the canonical Model Fabric vocabulary |
| `tools` | allowlist from the known tool set (nothing else is callable) |
| `permissions` | *requested* grants (`resource/operation/scope/risk/reason`); inert until an operator grants them |
| `model_requirements` | capabilities ⊂ declared capabilities, min context window, local/free/cost/latency bounds |
| `memory_policy` | retention (`none/session/persistent`), entry/count bounds; cross-agent reads can never be requested |
| `verification_requirements` | `require_tests/review/security_scan`, `min_benchmark_score` 0.0–1.0 |
| `resource_limits` | runs/hour, concurrency, tool calls/run, wall clock |

Validation is strict and re-runs on every load: corrupt stores fail
closed instead of loading partial state.

## Factory (`forge/agents/creation.py`)

`AgentCreationEngine.create_from_spec` / `create_from_template` validate
the spec and emit a structured `AgentPackage`: spec snapshot, semver
version, lifecycle state, executor binding, grants, version history,
benchmark history, and transition log, plus a stable `manifest()`.

Executor binding is honest: a package is `bound=True` only when its role
maps to an executor that genuinely exists (`coding/debugging/testing/
review/planning/security/research/documentation/game-development/
os-development`). Unbound packages are specifications — they can never be
enabled and the runtime refuses them.

## Lifecycle

```
created → validated → tested → enabled ⇄ paused
   ↓          ↓           ↓         ↓        ↓
 retired    disabled ← ──┴─────────┴────────┘
               ↓ (re-enable allowed)
            enabled
```

* `validate` re-validates the spec (`created → validated`).
* `benchmark` runs the suite; the score must clear the spec's
  `min_benchmark_score` (`validated → tested`).
* `enable` requires a bound executor and a passing benchmark.
* `retired` is terminal — nothing resurrects.

Only `enabled` packages may execute; the runtime checks this before any
subsystem runs.

## Mediated execution (`forge/agents/mediation.py`)

`GatedAgentRuntime` is the single choke point. Every run flows through:

1. **Model Fabric** — the only route to a model, bounded by the spec's
   model requirements. Without a fabric the run fails `NO_MODEL` instead
   of fabricating output.
2. **PolicyGate** — mutating tools (`filesystem_write`, `terminal`,
   `git`) are pre-authorized; denials fail the call with no side effects.
3. **Tool Runtime** — calls execute only through the runtime, only for
   allowlisted tools, inside the per-run tool budget.
4. **Memory** — namespaced per agent (`agent-<name>/`), bounded by the
   memory policy; cross-agent reads/writes raise `ISOLATION`.
5. **Verification** — output is scanned with the verification pipeline's
   exact secret/dangerous patterns plus the deterministic review gate;
   `require_tests` needs an explicit `test_command` or the run fails
   `TESTS_NOT_EXECUTED` (never pretends tests ran).
6. **Checkpoints** — `begin_mutation` captures a checkpoint before
   mutating sequences; `rollback` restores changed files on failure.

## No self-grants

* Spec permissions are requests. `grant_permission` / `revoke_permission`
  require a named approver distinct from `agent:<name>`; self-grants
  raise.
* `GatedAgentRuntime.run` refuses `approver == agent:<name>` (`SELF_GRANT`).
* Control-plane runs file `AGENT/execute` approvals under the agent's own
  identity, so the A33 store's approver≠agent rule refuses self-approval.

## Templates

`coding`, `research`, `security`, `game-dev` (`game-development`),
`os-dev` (`os-development`), `documentation` — least-privilege tool and
permission sets per role. Overrides merge key-wise and always re-validate.

## Benchmarks (`forge/agents/agent_bench.py`)

Deterministic, code-judged checks (never model-graded): `spec-valid`,
`lifecycle-gate`, `isolation-memory`, `permission-boundary`, plus
`model-smoke` when a fabric is attached. Missing subsystems report
`skipped`, never passed; the score covers executed checks only.

## Versioning

Explicit semver releases (`major/minor/patch`) with notes, actor,
timestamp, and spec hash; history is append-only and bounded. Exports
carry spec + versions only — never grants, never executors — and imports
arrive unbound and ungranted.

## CLI

```
forge agents [list] [--store PATH] [--json]
forge agents templates
forge agents create --template coding --name NAME [--set k=v] [--no-bind]
forge agents create --spec spec.json [--name NAME]
forge agents show|validate|test NAME
forge agents enable|pause|resume|disable|retire NAME
forge agents grant NAME INDEX [--approver NAME]
forge agents version NAME [--kind patch] [--notes ...]
forge agents run NAME --requirement "..." [--test-command ...]
```

The store defaults to `.forge/agent-engine.json` (atomic JSON).

## API + control plane

Audited, session-scoped endpoints under `/api/v1/engine/*`: `templates`,
`agents` (create/list), `agents/import`, `agents/{name}` (get/export),
`agents/{name}/{validate,test,enable,pause,resume,disable,retire,grant,
revoke,version,run}`. Runs evaluate `AGENT/execute` policy first
(`ALLOW` / `DENY` / approval flow), then execute synchronously through
the mediated runtime.

## Desktop

The Agents menu opens the **Agent Manager**: template + name creation,
agent list with state/version, one-click validate/test/enable/pause/
resume/disable/retire, full package JSON, and grant-safe backend calls —
all in worker threads over `DesktopBackend.agents_*`.

## Tests

`tests/test_creation_engine.py` (specs, templates, factory, lifecycle,
versioning, grants, export/import, persistence) and
`tests/test_creation_mediation.py` (isolation + permission boundaries,
verification, limits, checkpoints, benchmarks, plane/API/CLI/desktop
integration) — 62 tests. Isolation coverage: memory namespaces,
tool allowlists/budgets, gate denials, lifecycle refusals, self-grant
refusals at all three layers, hourly/concurrency limits, and policy
`DENY` on runs.
