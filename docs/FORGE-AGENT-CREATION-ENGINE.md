# Forge Agent Creation Engine (first-party)

Forge can now create new specialized software agents from structured
specifications — through one validated factory, one seven-state
lifecycle, and the same six subsystems that gate everything else.

## Specification (nine dimensions)

`forge/agents/spec.py` — `AgentSpec` carries exactly:

- `name` (`[a-z][a-z0-9_-]{2,48}`)
- `purpose` (1–1000 chars)
- `capabilities` (1–12, canonical Model Fabric vocabulary)
- `tools` (0–16, closed tool vocabulary)
- `permissions` (0–16 explicit `resource/operation/scope/effect` grants;
  terminal grants must pin a concrete scope)
- `model_requirements` (`capability` + routing hints)
- `memory_policy` (`max_entries`, `max_value_chars`, mandatory
  `isolated: true`)
- `verification_requirements` (≥1 of `tests/build/lint/security/review`)
- `resource_limits` (`max_runs_per_hour`, `max_concurrent`, `max_seconds`)

Validation is strict and fail-closed. A spec grants nothing: requested
permissions become effective only through operator approval.

## Factory → structured package

`forge/agents/creation_engine.py` — `AgentCreationEngine.create()` turns a
validated spec into an `AgentPackage` (`forge/agents/package.py`):
spec snapshot, lifecycle state, semver version, version history,
benchmark evidence, and provenance. Packages are plain JSON
(`forge-agent-package` v1) — no secrets, no executors, no live handles.
The engine is session-scoped in the control plane and file-backed
(`.forge/agents/*.json`) for the CLI.

## Lifecycle (seven states)

`forge/agents/lifecycle.py`:

```text
created -> validated -> tested -> enabled <-> paused
                                enabled -> disabled -> enabled
                                * -> retired (terminal)
```

Skipped steps are refused; `retired` is terminal. Any spec change
(including permission grants) bumps the version and resets to `created`,
so validation and benchmarking must be re-earned.

## Operation through the six subsystems

`forge/agents/managed_executor.py` — every run carries evidence from:

- **Model Fabric** (routed by the spec's capability),
- **PolicyGate** (writes evaluated before the runtime sees them),
- **Tool Runtime** (calls allowlisted to the spec's tools),
- **Memory** (run records in the agent's own namespace only),
- **Verification** (each declared gate executed, pass/fail recorded),
- **Checkpoints** (snapshot before the first write tool).

Only `enabled` agents run; quotas, policy denials, allowlist violations,
and verification failures refuse honestly with no partial effects.

## No self-grant

Every mutation requires an operator `actor` distinct from the agent's own
identity (`name`, `forge-managed:name`, `agent:*`). Self-grants,
self-transitions, self-updates, and self-approvals are rejected, as are
grant-shaped tool calls (`*grant*`, `*permission*`, `*approve*`,
`*escalat*`). Agents may only *request* permissions
(`request_permission` returns a receipt with `granted: false` for an
operator to decide).

## Templates

`forge/agents/templates.py`: `coding`, `research`, `security`,
`game-dev`, `os-dev`, `documentation`. Templates are starting specs, not
power — instantiation still walks the full lifecycle.

## Benchmarks

`forge/agents/agent_benchmark.py` — eight deterministic checks judged by
code (`spec_valid`, `lifecycle_valid`, `tools_allowlisted`,
`permissions_bounded`, `memory_isolated`, `model_routable`,
`verification_declared`, `resource_limits_bounded`). Reports are stored
on the package; failures block `tested` (and therefore `enabled`).

## Versioning

Strict semver with monotonic increases and a bounded history of prior
spec snapshots (`actor`, `reason`, `at`). `bump`/`set_version` reset
lifecycle to `created`.

## Surfaces

- **Control plane**: `managed_*` methods (audited under
  `managed-agents`); `managed_run` pre-gates `AGENT/execute` and files
  approvals exactly like legacy runs.
- **API**: `/api/v1/agents/managed/*` (templates, CRUD, validate, test,
  enable/pause/disable/retire, version, grant, run, approvals).
- **CLI**: `forge agents [list|templates|create|show|validate|test|
  enable|pause|disable|retire|version|grant]` (bare `forge agents`
  lists).
- **Desktop**: Agents menu → Agent Manager (list, create from template,
  validate, test with benchmark window, lifecycle actions).

## Testing

`tests/test_forge_agent_creation_engine.py` (49): spec validation,
factory/persistence, lifecycle walks and refusals, six-subsystem
execution (fabric/memory/verification/tools/checkpoint/governor),
self-grant/self-transition/self-approval refusals, templates,
benchmarks, versioning, control-plane + API flows, memory/session
isolation, permission boundaries, CLI, and desktop backend + window.

Full suite: **1872 passed, 3 skipped**.
