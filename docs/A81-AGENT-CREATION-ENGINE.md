# A81 — Agent Creation Engine

Forge can now create specialized software agents from structured
specifications — as a first-party engine, not a plugin.

A specification is an operator-authored contract. The
**Agent Creation Factory** turns it into a structured, versioned
**agent package**; the package walks an explicit lifecycle; and once
enabled, the agent runs strictly through the same six subsystems every
Forge agent shares. Creating an agent grants nothing: its permissions
are a *ceiling* the PolicyGate may only tighten, and no agent may ever
grant or approve anything for itself.

## The specification (nine fields)

`forge/agents/engine/spec.py` — `AgentSpecification`, frozen and
validated against the canonical Forge vocabularies:

| Field | Validation |
|---|---|
| `name` | `^[a-z][a-z0-9_-]{2,48}$` |
| `purpose` | non-empty, ≤ 500 chars |
| `capabilities` | ≥ 1, ≤ 12, from the A31 19-capability vocabulary |
| `tools` | ≤ 12, from the runtime tool vocabulary |
| `permissions` | ≤ 16, `resource:operation` pairs from the A33 `RESOURCE_OPERATIONS` table |
| `model_requirements` | capabilities, min context, local/paid/remote posture, routing preset |
| `memory_policy` | enabled, scope (`agent`), max entries, explicit cross-agent sharing |
| `verification` | which gates run (tests / review / security / require-all) |
| `resource_limits` | runs/hour, concurrency, tool calls/run, output tokens, timeout |

Coupling rule: **every tool must be covered by its mapped permission
scope** — a `write_file` tool without `filesystem:write` in the ceiling
is refused at specification time. Specs are immutable at runtime.

## The factory and the package

`forge/agents/engine/factory.py` — `AgentCreationFactory.create()`
validates the spec and emits an `AgentPackage`
(`forge-agent-package` format v1): stable `agent_id`, the frozen spec,
current version + immutable history, lifecycle status, provenance, and
the six subsystem descriptors (Model Fabric, PolicyGate, Tool Runtime,
Memory, Verification, Checkpoints). Packages persist under
`.forge/agents/<name>/` (`package.json`, immutable `versions/vN.json`,
per-agent `memory/`) through the path-safe, atomic `PackageStore`.

## The lifecycle

```
created → validated → tested → enabled ⇄ paused
                                │
        (any live state) ───────┴→ disabled → retired (terminal)
```

- `validated` requires deterministic spec validation.
- `tested` requires a **passed agent benchmark** (below).
- `enabled` is the single runnable state — every other state refuses
  to dispatch, including `paused` and `disabled`.
- `disabled` must re-pass the benchmark (`disabled → tested`) before
  it can be re-enabled.
- `retired` is terminal; only retired packages can be deleted.

## Agent versioning

Every accepted spec change appends a new immutable version
(`VersionHistory`, bounded to 20). **An update resets the lifecycle to
`created`** — a changed agent never inherits its old clearance and
must be re-validated, re-tested, and re-enabled. Rollback does not
mutate history: it appends a new version copying an old snapshot.

## Operating through the six subsystems

`forge/agents/engine/runtime.py` — `EngineRuntime.run()` dispatches
one task through, in order:

1. **Model Fabric** — the request routes under the spec's model
   requirements; the response is untrusted text.
2. **PolicyGate** — every write, tool call, and permission question is
   evaluated under the agent's identity (`agent:<name>`); DENY is
   never escalated.
3. **Tool Runtime** — tools run only through the permissioned runtime
   and only when in the spec's allowlist *and* covered by the ceiling.
4. **Memory** — per-agent namespace under `.forge/agents/<name>/memory`
   (or the explicit opt-in shared pool); traversal-checked keys.
5. **Verification** — the spec's gates (security / review / tests) run
   over exactly the files the agent wrote.
6. **Checkpoints** — file changes checkpoint before the first write and
   roll back exactly the agent's files when verification fails.

Changes apply through the same `ChangeApplier` transaction the coder
uses (validate → authorize → checkpoint → write), so protected paths
(`.git`, `.forge`, `.env`, credentials) are refused exactly as
everywhere else in Forge.

## Isolation and permission boundaries (all benchmark-tested)

- **Tool boundary** — spec allowlist ∩ registered tools; anything else
  is refused and recorded.
- **Permission ceiling** — operations outside `spec.permissions` are
  *hard* refusals; approval can never expand the ceiling. Inside it,
  the A32/A33 PolicyGate still decides (ASSISTED still needs approval).
- **Memory boundary** — physically separate namespaces per agent;
  traversal keys rejected; sharing is explicit opt-in.
- **Lifecycle boundary** — only `enabled` dispatches.
- **Resource boundary** — per-agent run/hour, concurrency, and
  per-run tool-call budgets.
- **No self-grant** — specs are frozen; agent actors (`agent:<name>`,
  or an actor sharing the agent's name) are refused on every mutating
  factory path and cannot approve permission requests (refusals are
  recorded and audited); the control plane refuses `agent:`-shaped
  session actors outright.

## Agent benchmark testing

`forge/agents/engine/benchmark.py` — nine deterministic checks judged
by code on real engine objects, never by the agent: spec-validation,
lifecycle-gating, tool-boundary, permission-ceiling, policy-gate,
self-grant-prohibition, memory-isolation, model-routing (honest
failure when no model can serve the requirements), resource-limits.
A package only advances to `tested` when the suite passes; failures
are reported as failures.

## Templates

Six reviewed starting points (`forge/agents/engine/templates.py`):
**coding**, **research** (read-only), **security** (audit-only),
**game-dev**, **os-dev** (the most conservative budgets), and
**documentation**. A template is a spec skeleton — instantiating one
still requires the full lifecycle before anything can run.

## CLI

```bash
forge agents                                  # list packages
forge agents templates                        # show the six templates
forge agents create --template coding --name my-coder --purpose "…"
forge agents create --file spec.json          # full spec payload
forge agents validate my-coder                # created → validated
forge agents test my-coder                    # benchmark; → tested
forge agents enable my-coder                  # operator action
forge agents run my-coder "add a helper" [--approve]
forge agents pause|resume|disable my-coder
forge agents versions my-coder                # version history
forge agents rollback my-coder 1              # re-promote as a new version
forge agents retire my-coder && forge agents delete my-coder
```

## Desktop Agent Manager

The desktop app (`forge desktop`) gains an **Agents** tab: create from
a template, validate / test / enable / pause / resume / disable /
retire, run a task (with optional pre-approval), and inspect the full
package manifest, version history, and benchmark results. All manager
calls go through the headless `DesktopBackend` → control plane →
engine path on background threads; the UI never touches the engine
directly.

## Control plane

`ControlPlane` exposes the engine per project (audited under
`agent-packages`): `agent_packages`, `agent_package_templates`,
`agent_package_create/update/rollback/validate/test/transition/delete/
versions/get/run`. Agent-shaped actors are refused for every mutation
and the denial is audited. Packages persist in the project store
across restarts.

## Security notes

- Permissions in a spec are a ceiling, never a grant; the PolicyGate
  can only tighten them.
- Model output is untrusted: proposals outside the ceiling refuse the
  whole run (nothing is written), and protected paths are refused by
  the change layer.
- Verification failure rolls back exactly the agent's files; unrelated
  work is never touched.
- The engine adds boundaries on top of A32/A33 — it can only restrict
  further, never loosen.

## Testing

Six suites, 53 tests:

- `tests/test_a81_engine_spec.py` (10): nine-field validation,
  vocabularies, tool-permission coupling, frozen specs, six templates.
- `tests/test_a81_engine_lifecycle.py` (7): transition table,
  terminal retirement, full lifecycle walk, append-only versioning
  with lifecycle reset, store roundtrip/bounds, delete-requires-retire.
- `tests/test_a81_engine_runtime.py` (16): no-self-grant (factory,
  permission expansion, self-approval), permission ceiling + gate
  layering, tool allowlist, out-of-ceiling model proposals refused
  wholesale, memory isolation/policies, lifecycle gating per state,
  resource limits, the six-subsystem run, verification rollback,
  text-only answers, untrusted output, protected paths.
- `tests/test_a81_engine_benchmark.py` (6): pass case, honest
  no-model failure, broken-spec failure, enabled-lifecycle probe,
  shared/disabled memory policies.
- `tests/test_a81_cli.py` (6): full CLI lifecycle, JSON output,
  refusal paths, spec-file creation, rollback.
- `tests/test_a81_plane_desktop.py` (8): control-plane lifecycle,
  agent-actor refusals + audit, validation errors, desktop backend
  manager, UI contracts, cross-restart persistence.

A81 result: **53 new tests; full suite 1876 passed, 3 skipped** (A80
baseline: 1823 passed, 3 skipped).
