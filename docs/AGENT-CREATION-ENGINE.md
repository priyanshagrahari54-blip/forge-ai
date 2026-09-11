# Forge Agent Creation Engine

Forge creates new specialized software agents from structured
specifications — never from prompts, claims, or vibes.

## Specification

`forge/agents/spec.py` — `AgentSpec` carries the nine required fields:

- `name` — `[a-z][a-z0-9_-]{2,48}`
- `purpose` — 1–1000 chars of honest intent
- `capabilities` — 1–12 from the canonical Model Fabric vocabulary
- `tools` — allowlisted Tool Runtime names only
- `permissions` — allowlisted A32 operations only; `delete_repository`
  and `expose_secrets` are rejected at validation (fail closed)
- `model_requirements` — capabilities, local/free preference,
  minimum context window, optional cost ceiling
- `memory_policy` — entry/value bounds, project-memory opt-in,
  retention
- `verification_requirements` — tests/security/review gates plus the
  minimum benchmark pass rate
- `resource_limits` — runs/hour, concurrency, seconds/run,
  output chars, files/run

## Factory

`forge/agents/package.py` — `build_package(spec)` generates the
structured agent package: spec + semantic version + lifecycle state +
executor binding + test report + version history. The `manifest()`
wires the six subsystems every run passes through:

Model Fabric, PolicyGate, Tool Runtime, Memory (namespaced per
agent), Verification, Checkpoints.

`real` is True only when the package binds a registered executor for
one of its capabilities; anything else is an honest specification
that cannot run tasks.

## Lifecycle

`forge/agents/lifecycle.py` — seven states:

```
created -> validated -> tested -> enabled
                                  enabled <-> paused
                                  enabled/paused/tested/... -> disabled
                                  * -> retired (terminal)
```

Only `enabled` agents run. Enabling requires `tested` (a passing
benchmark) or a previous enable (`paused`/`disabled`). `retired` is
terminal.

## No self-grant

`forge/agents/engine.py` — every power-changing operation (`create`,
`update`, `validate`, `test`, `enable`, `pause`, `disable`,
`retire`, `update_permissions`, `set_limits`, `delete`) requires an
operator identity that differs from the agent's own name (including
its internal `forge-agent:<name>` identity). An agent acting as its
own operator gets `SelfGrantDenied`. Agents are never handed the
engine, so running code cannot escalate even by trying.

## Templates

`forge/agents/templates.py` — six first-party starting points:

- `coding` — gated change sets with tests + review
- `research` — read-only evidence gathering
- `security` — read-only audits (secrets, dangerous patterns)
- `game-development` — game code/scenes with playable verification
- `os-development` — OS components with tight budgets
- `documentation` — docs from real repository content

## Benchmarks

`forge/agents/agent_benchmarks.py` — deterministic checks judged by
code, never by models: manifest completeness, bounded
capabilities/tools/permissions, lifecycle validity, memory-namespace
isolation, resource limits, routable model requirements, and binding
honesty. Live Model Fabric routing checks are opt-in (`live=True`);
otherwise they are recorded as `skipped`, never passed by
assumption.

## Versioning

`forge/agents/versioning.py` — strict `major.minor.patch`. Every spec
change bumps the version, appends a bounded history entry (who, what,
when), and resets the package to `validated` so the new spec must
re-test before it can run.

## Running

`AgentCreationEngine.execute()` runs an `enabled` + bound package:

1. quota check (`AgentGovernor` from the spec's limits)
2. pre-run checkpoint
3. Model Fabric routing under the spec's model requirements
4. capability worker (model-driven change sets / deterministic
   security-test-review scan / deterministic repository analysis)
   through the scoped Tool Runtime + PolicyGate
5. VerificationPipeline security + review gates over changed files
6. rollback of candidate files on failure

Tool calls need both the listed tool *and* its permission; missing
either fails closed. Memory is one `MemoryStore` per agent with the
spec's entry/value bounds enforced.

## Surfaces

- CLI: `forge agents` (list), `create`, `show`, `validate`, `test`,
  `enable`, `pause`, `disable`, `retire`, `delete`, `versions`,
  `run`, `templates` — file store at `.forge/agents` by default
- API: `/api/v1/agent-specs/...` (templates, CRUD, lifecycle,
  permissions, versions, run) — session-scoped like the A49 factory
- Desktop: Agents menu → Agent Manager (create from template,
  validate, test, enable, pause, disable, retire, delete)
- Control plane: `spec_agent_*` methods with the same AGENT/execute
  gating and audit as direct agent runs

## Testing

`tests/test_forge_agent_engine.py` (38): spec validation, templates,
packages, lifecycle, versioning, benchmarks, no-self-grant,
memory/tool/permission/run isolation, write-approval boundaries,
governor quotas, six-subsystem execution, rollback, file store, CLI,
control plane (+ session isolation, DENY fail-closed, legacy name
collisions), API, and the desktop Agent Manager backend.
