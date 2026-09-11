# A81 — Forge Agent Creation Engine

Forge can create new specialized software agents from structured
specifications. A created agent is not a prompt string: it is a
validated, content-addressed **package** with an explicit permission
envelope, a derived runtime plan, a lifecycle, a version, and a
code-judged benchmark record.

```text
specification -> factory -> package -> validation -> benchmark
             -> operator enable -> bound agent -> Model Fabric
             -> PolicyGate -> Tool Runtime -> Memory -> Verification
             -> Checkpoints
```

## 1. Agent specification (`forge.agent_engine.spec`)

`AgentSpec` is the only input to the factory. It is strictly validated
and content-addressed (`fingerprint()`), so a package always traces
back to the exact spec that produced it.

| Field | Meaning |
| --- | --- |
| `name` | stable identifier, `[a-z][a-z0-9_-]{2,48}` |
| `purpose` | human-readable intent (≤ 500 chars, required) |
| `capabilities` | canonical Model Fabric capabilities only |
| `tools` | tool names the agent may *request* |
| `permissions` | `PermissionSpec`: read/write scopes, network, terminal, commit, domains, per-write approval |
| `model_requirements` | `ModelRequirements`: capability, context window, output tokens, local/free preference, complexity, fallback |
| `memory_policy` | `MemoryPolicy`: scope (`none`/`task`/`session`/`persistent`), namespace, entry/byte budgets, TTL |
| `verification` | `VerificationRequirements`: required gates, checkpoint, independent review, repair attempts |
| `resource_limits` | `ResourceLimits`: runs/hour, concurrency, wall clock, tokens, files touched, bytes written |

Refused at parse time, always:

- unknown fields, unknown capabilities, unknown gates, out-of-range limits;
- scopes that are absolute, use `..`, or touch `.git`/`.forge`;
- escalation tools (`grant_permission`, `self_grant`, `sudo`, `escalate`, …);
- `memory_policy.allow_secrets = true` — agent memory may never hold secrets;
- domains without `allow_network`; write scopes with no tool that writes;
- a primary model capability that is not also declared in `capabilities`.

The `security` verification gate is mandatory and is re-added if a spec
omits it.

## 2. Agent factory (`forge.agent_engine.factory`)

`AgentFactoryEngine.build()` is deterministic and side-effect free: the
same spec always yields the same runtime plan, prompt, and
`package_id`. The package (`forge.agent_engine.package`) contains a
manifest, the exact spec, the runtime plan, the derived system prompt,
and the lifecycle state — never secrets and never executable code.

The runtime plan is derived **only** from the spec, one section per
subsystem: `model_fabric`, `policy_gate`, `tool_runtime`, `memory`,
`verification`, `checkpoints`, `resource_limits`. Each declared tool is
mapped to the runtime operation the permission platform will be asked
to authorize; a tool needing power the spec did not grant (write,
network, terminal, commit) is refused at build time, and unrecognized
tools are reported under `unknown_tools` rather than silently granted.

`PackageValidator` (`forge.agent_engine.validator`) then re-checks the
built package against its own spec — widened scopes, dropped approvals,
dropped gates, mismatched routing, or a `self_grant` claim are
`critical` findings that make the package invalid.

## 3. Agent lifecycle (`forge.agent_engine.lifecycle`)

```text
created ──► validated ──► tested ──► enabled ⇄ paused
   │            │            │          │
   └────────────┴────────────┴──────────┴──► disabled ──► validated
                                          └────────────► retired (terminal)
```

- Only a **tested** package with a passing validation and a passing
  benchmark may be enabled.
- `disabled` must be re-validated and re-tested before enabling again;
  `paused` may resume directly.
- `retired` is terminal — no resurrection.
- An agent may never change its own state: `transition()` refuses when
  the actor is the agent, and `enabled`/`retired` refuse any
  `agent:`-prefixed actor. Every transition is recorded with its actor.

## 4. Operating through the platform (`forge.agent_engine.runtime`)

`BoundAgent` is the only way a created agent acts:

- **Model Fabric** — `think()` builds a `ModelRequest` from the routing
  plan; the agent cannot choose a provider. Prompt and context are
  redacted before they leave.
- **PolicyGate** — `authorize()` consults the gate for every action
  with operation/path/tool/risk, after checking the package's own
  scopes and approval requirement. Denials are recorded on the run.
- **Tool Runtime** — `use_tool()` executes only after authorization,
  through the permissioned runtime.
- **Memory** — per-agent namespace (`agents/<name>/…`), bounded
  entries/bytes, TTL expiry, key traversal refused, secret-bearing
  content refused.
- **Verification** — `verify()` runs exactly the declared gates.
- **Checkpoints** — `begin_run()` checkpoints before the first write;
  `rollback()` restores only the run's files and leaves unrelated work
  alone.

## 5. No agent may self-grant permissions

Enforced in five independent places:

1. Escalation tools are refused at spec parse time.
2. The factory refuses to wire a tool beyond the declared envelope.
3. The validator flags any runtime section wider than the spec.
4. `BoundAgent.request_grant()` always raises; there is no other grant
   API on the agent, the engine, or the desktop manager.
5. Widening a spec is classified `MAJOR`, which resets the lifecycle to
   `created` — power can never grow without operator re-validation and
   re-testing.

## 6. Templates (`forge.agent_engine.templates`)

`coding`, `research`, `security`, `gamedev`, `osdev`, `documentation`.
Every template is conservative: writes require approval, terminal and
commit are off, network is off unless the purpose needs it (and then
only for declared domains), and the security gate is present. Templates
are validated by the same rules as any other spec, and overrides are
re-validated (they cannot smuggle in an escalation tool).

## 7. Agent benchmark testing (`forge.agent_engine.benchmark`)

An agent is never trusted to report its own competence — every check is
judged by Python.

*Static* (always): package validity, self-grant refusal, scope
isolation against protected paths, undeclared-tool refusal, write-scope
enforcement, memory namespacing, memory secret refusal, mandatory
security gate, bounded resource limits, and a prompt that states the
rules.

*Behavioural* (only with a bound fabric): the package must actually
route a request and return non-empty, secret-free text.

The pass threshold is 1.0 — every check must pass. A crashing check is
a failing check.

## 8. Agent versioning (`forge.agent_engine.version`)

Versions are `MAJOR.MINOR.PATCH`, and the bump level is **derived from
the spec diff**, never declared by the caller:

- **MAJOR** — any widening (network/terminal/commit, wider scopes or
  domains, new tools, raised limits, dropped approval), a removed
  capability, or weakened verification.
- **MINOR** — behavioural change with no widening.
- **PATCH** — cosmetic only.

`engine.revise()` records the entry, rebuilds the package, and resets
the lifecycle. `rollback_to()` restores an older package as `created`,
so a rollback is retested too.

## 9. CLI

```bash
forge agents                              # list agents and states
forge agents templates                    # first-party templates
forge agents create --template coding     # create + validate
forge agents create --spec spec.json      # create from a JSON spec
forge agents show NAME
forge agents validate NAME
forge agents test NAME [--no-model]
forge agents enable NAME
forge agents disable NAME
forge agents pause NAME | retire NAME
forge agents versions NAME
```

Every subcommand takes `--json` and `--store PATH` (default
`.forge/agents/agents.json`). Exit code `0` = success, `1` = refused
(invalid, failed benchmark, illegal transition), `2` = usage/IO error.

## 10. Desktop Agent Manager

`forge.desktop_app.agent_manager.AgentManager` is the headless,
GUI-free logic (list, create from a template, validate, test, enable,
pause, disable, retire, plus a rendered envelope summary). The Tk app
adds an **Agents** tab wired to it. Both share the CLI's JSON store, so
the terminal and the desktop always agree. The manager exposes no
permission-granting API.

## 11. Tests

| Suite | Focus |
| --- | --- |
| `tests/test_a81_agent_spec.py` | specification validation and refusals |
| `tests/test_a81_agent_factory.py` | deterministic packaging, envelope fidelity, export/import trust reset |
| `tests/test_a81_agent_lifecycle.py` | state machine, gating, derived version bumps |
| `tests/test_a81_agent_benchmark.py` | code-judged benchmark, all six templates |
| `tests/test_a81_agent_isolation.py` | **isolation and permission boundaries** |
| `tests/test_a81_agent_cli.py` | `forge agents` CLI and the desktop Agent Manager |
| `tests/test_a81_agent_e2e.py` | full path through the real subsystems |

The isolation suite drives a real `BoundAgent` against a real
`PolicyGate`, `ToolRuntime`, `CheckpointManager`, and
`VerificationPipeline` in a temporary repository and asserts the ten
invariants listed in its module docstring.

## Not included

- No agent may execute arbitrary shell commands through a template; the
  `terminal` tool exists in the mapping but no first-party template
  grants it.
- The engine does not schedule or run agents autonomously; it creates,
  gates, and binds them. Task orchestration remains the Supervisor's
  job.
