# Agent Creation Engine (A81)

First-party engine for creating specialized software agents from
structured specifications. Agents are produced by the factory, moved
through a centrally enforced lifecycle by the manager, and execute
through the six core platform services — Model Fabric, PolicyGate,
Tool Runtime, Memory, Verification, and Checkpoints — and through
nothing else.

## The specification

Every agent starts as an `AgentSpec`
(`forge/agent_engine/spec.py`) with nine parts:

| Field | Meaning |
| --- | --- |
| `name` | stable identity, `[a-z][a-z0-9-]{2,48}` (immutable across versions) |
| `purpose` | what the agent exists to do (bounded, required) |
| `capabilities` | canonical Model Fabric capability vocabulary |
| `tools` | the exact tool surface the agent may call |
| `permissions` | the exact permission operations granted |
| `model` | model requirements: minimum capabilities, provider preference, context floor, fallback policy |
| `memory` | memory policy: enabled, entry/entry-size bounds, retention (`version` or `forever`) |
| `verification` | benchmark id, minimum score, minimum scenarios, tests/security-review flags |
| `limits` | hard resource limits: tokens/request, requests, wall clock, files written, working directories |

Validation is fail-closed: any violation raises `SpecError` listing
every issue. Extra permissions beyond those implied by the declared
tools require an explicit per-permission rationale, and
`delete_repository` / `expose_secrets` can never be granted to any
created agent.

## The factory

`AgentFactory` (`forge/agent_engine/factory.py`) turns a validated
specification into a structured, versioned package on disk:

```
.forge/agents/<name>/
  current.json                    # {version, lifecycle}
  versions/v<N>/
    agent.json                    # immutable manifest
    spec.json                     # the specification
    permissions.lock              # granted set + digest
    benchmarks.json               # latest benchmark run
  history.jsonl                   # append-only audit
```

Version directories are written exactly once (immutable). The
no-self-grant rule lives in the factory: a new version may keep or
shrink its permission set freely, but growing it is an escalation
that is refused with `PermissionEscalationError` unless a human
operator explicitly confirms it (`--confirm-escalation` in the CLI) —
and even then it must pass full spec validation and is permanently
recorded in the audit history. Agents hold no reference to the
factory.

## The lifecycle

The manager (`forge/agent_engine/manager.py`) is the only lifecycle
authority:

```
created → validated → tested → enabled ⇄ paused
                                ↓          ↓
                              disabled → retired (terminal)
```

- Only `tested` agents may be enabled; only the latest version may be
  current.
- A failed re-test demotes `tested` → `validated` honestly.
- `disabled` agents re-enter only via validate → test → enable.
- `retired` is terminal and irreversible.

Agents never receive a reference to the manager: their runtime's
`enabled` flag is decided from the store's recorded state, so an
agent can neither enable itself nor grant itself permissions.

## The runtime

`AgentRuntime` (`forge/agent_engine/runtime.py`) is the agent's
entire execution surface:

- **Model Fabric** — `call_model` routes through a `ModelFabric`
  (or stand-in), carrying the spec's required capabilities and
  context floor.
- **PolicyGate** — every tool call is evaluated against an
  `AgentPermissionManager` whose fallback for anything outside the
  grant is BLOCKED (never approval-required). DENY is absolute.
- **Tool Runtime** — holds exactly the spec's declared tools;
  undeclared tools do not exist for the agent.
- **Memory** — `remember`/`recall` confined to
  `agents/<name>/<scope>/` with hard entry and size bounds; retention
  decides whether the scope is per-version (`v<N>`) or shared.
- **Verification** — `verify` runs `VerificationPipeline` and reports
  every gate.
- **Checkpoints** — `checkpoint`/`rollback` through
  `CheckpointManager` restore exact pre-change bytes.

Approval is constructor-only: the operator supplies an `approver`
callable; the agent API has no way to approve itself, so writes and
commands never self-approve. Writes are confined to the spec's
`working_dirs`, protected paths (`.env`, `.git`, `.forge`, …) are
always denied, and exhausting any resource budget refuses further
work for the session (`AgentLimitError`).

## Benchmarks

`AgentBenchmarkHarness` (`forge/agent_engine/benchmarks.py`) runs
deterministic, offline, code-judged scenarios per benchmark id
(`generic` plus one per template). Scenarios drive the real runtime:
spec integrity, memory scope, tool boundary, permission boundary,
model routing (against a recorded stand-in fabric — never a live
model), checkpoint rollback, resource accounting, and
template-specific layouts (code layout, notes memory, secret guard,
assets/scenes, command boundary, docs layout). The result gates the
`tested` state at the spec's minimum score/scenarios, and is stored
with the version.

## Templates

Six built-in least-privilege templates
(`forge/agent_engine/templates.py`):

| Template | Surface |
| --- | --- |
| `coding` | read/write/tests/search/git; confined to `src`, `tests`, `lib` |
| `research` | read-only: read/search/git status |
| `security` | read-only audit surface + tests; security review required |
| `game-dev` | code + assets: `src`, `assets`, `scenes`, `tests` |
| `os-dev` | adds approved `run_command` inside `src`, `kernel`, `scripts`, `tests` |
| `documentation` | writes confined to `docs/` |

## CLI

```bash
forge agents                              # list agents
forge agents create NAME --template coding
forge agents create NAME --spec spec.json
forge agents templates
forge agents validate NAME
forge agents test NAME [--version N]
forge agents enable NAME                  # only from tested
forge agents pause|resume|disable|retire NAME
forge agents show NAME [--version N]
forge agents versions NAME
forge agents export NAME [--out file.json]
forge agents update NAME --spec spec.json [--confirm-escalation]
```

Every command accepts `--json`; the store defaults to `.forge/agents`
(`--agents-dir` to override).

## Desktop

The desktop app adds an **Agent Manager** (`Agents` menu →
`Agent Manager…`): per-project agent list with lifecycle and
runnable state, create-from-template dialog, template catalog,
and Validate / Test / Enable / Pause / Resume / Disable / Retire
actions. The window is a thin view over `DesktopBackend` — every
state change goes through the manager and the lifecycle machine.

## Isolation & permission boundaries (verified)

The test suite (`tests/test_a81_agent_engine_*.py`) pins the
boundaries:

- undeclared tools are refused (they do not exist in the runtime);
- non-granted operations are BLOCKED, and approval cannot lift them;
- writes and commands never execute without the operator's approver;
- protected paths are denied even with a fully approving operator;
- writes stay inside the declared working directories;
- memory is namespaced per agent and per version scope; traversal
  keys are refused;
- token, request, file-write, entry, and wall-clock budgets are hard;
- escalations are refused without operator confirmation and audited
  when confirmed;
- disabled/paused agents cannot execute anything;
- package tampering is detected by digest verification.
