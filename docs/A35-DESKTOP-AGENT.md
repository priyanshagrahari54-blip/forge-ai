# A35 — Desktop Agent (controlled execution architecture)

A35 turns the A33 desktop *permission* foundation into a **controlled
execution architecture**. The agent may observe and act on a desktop — but
every single action, including pure observations, flows through the A33
permission system, deterministic risk classification, hard security
invariants, a permission profile, and (for actuation) the A33 approval
store. There is no unrestricted desktop control anywhere in this package.

```text
COCKPIT / TASK / BRIDGE CALLER
 ↓
DesktopRequest (structured action vocabulary + validation)
 ↓ 1. agent identity            fail closed when missing
 ↓ 2. task scope                GrantScopeChecker (explicit grants, TTL)
 ↓ 3. A33 PolicyGate            PermissionPolicy.evaluate — single authority
 ↓ 4. risk + hard invariants    classify(); invariants ALWAYS deny
 ↓ 5. profile                   SAFE / ASSISTED / AUTONOMOUS / CUSTOM
 ↓ 6. approval                  A33 ApprovalStore (single-use tokens)
 ↓ 7. provider execution        DesktopProvider protocol
 ↓ 8. observation               redacted, deterministic on the fake desktop
 ↓ 9. audit                     every step recorded, nothing executable skipped
```

## What A35 adds

| Area | Location | Notes |
|---|---|---|
| Action vocabulary | `forge/desktop/actions.py` | 15 structured kinds, bounded validation |
| Provider protocol | `forge/desktop/provider.py` | Backend-agnostic `DesktopProvider` + deterministic `FakeDesktopProvider` |
| Risk + invariants | `forge/desktop/risk.py` | NONE→CRITICAL scale, five always-deny invariant families |
| Profiles | `forge/desktop/profiles.py` | SAFE / ASSISTED / AUTONOMOUS / CUSTOM (tighten-only) |
| Execution agent | `forge/desktop/agent.py` | The gated pipeline; never executes outside it |
| Bridge boundary | `forge/desktop/bridge.py` | Authenticated server↔provider boundary with session scopes |
| Control plane | `forge/control/control_plane.py` | Session-bound desktop views, act/check/grant/decide |
| API | `forge/api/routes_commands.py`, `schemas.py` | `/api/v1/desktop/*`, rate-limited mutations |
| Cockpit | `forge/cockpit/web/` | Desktop view: state, capability matrix, action form, approvals |
| Tests | `tests/test_a35_*.py` | 9 suites: actions, provider, risk, profiles, agent, security, UI, E2E, control plane |

## Action vocabulary

`DesktopActionKind` (15 kinds). Observation kinds are marked `OBSERVATION`;
every kind maps onto the A33 `Resource.DESKTOP` operation vocabulary
(`read_screen`, `screenshot`, `mouse_move`, `mouse_click`, `keyboard`,
`launch`, `window`, `clipboard`, `file_access`, `process`, `system_info`).

| Kind | Type | What it does |
|---|---|---|
| `screenshot` | observation | Raster-free screen state, focused window |
| `read_screen` | observation | Active-window title/app detail |
| `window_list` | observation | All windows |
| `window` | actuation | focus/minimize/maximize/close/move/resize |
| `mouse_move` / `mouse_click` | actuation | Pointer control (bounded coordinates) |
| `keyboard` | actuation | Typed text or key chords (bounded length) |
| `launch` | actuation | Open an application, optional args |
| `file_select` | observation | Choose a file for authorized file ops |
| `file_access` | actuation | Read/write/delete within declared scope |
| `clipboard` | actuation | Read/write clipboard |
| `app_action` | actuation | Application-specific action (structured params) |
| `process_list` / `process` | observation | Process table / process detail |
| `system_info` | observation | Machine facts (never credentials) |

`validate_request()` enforces structural bounds before anything else runs:
target ≤ 300 chars, keyboard text ≤ 2000, clipboard content ≤ 10 000,
launch args ≤ 16 with no shell metacharacters, coordinates ≤ 20 000, params
depth ≤ 6, file paths relative-only. A request that fails validation is
DENIED — validation is the first gate, not a precondition.

## Risk classification and hard invariants

`classify(request)` assigns NONE/LOW/MEDIUM/HIGH/CRITICAL from the action's
base risk plus parameter raises (launch args, file writes, process
termination, clipboard writes, typing into a terminal). Unknown/ambiguous
risk fails closed at MEDIUM (approval), never at AUTO.

`hard_violations(request)` is deterministic and independent of profiles,
policy, and tokens — **no profile can override it**. Five families:

1. **Credential extraction** — targets or arguments touching credential
   stores/password managers, or paths into credential material
   (`~/.ssh`, `.aws/credentials`, `id_rsa`, …).
2. **Security-control disabling** — killing/closing/disabling security
   software, including kill utilities pointed at security processes
   (`taskkill /im defender.exe`, `pkill falcon`, …).
3. **Privilege escalation** — `sudo`, `su`, `pkexec`, `runas`, `doas`-style
   content in launch arguments or typed text.
4. **Unauthorized persistence** — writing autostart/systemd/cron/launchd
   entries, or launching persistence schedulers.
5. **Unauthorized remote control** — launching remote-access/tunneling
   servers.

The escalation check inspects typed keyboard text as well as launch
arguments, because "type `sudo rm -rf /` into a terminal" is the same
privilege boundary crossed through a different door.

## Profiles

Profiles gate *prompt behavior only*; policy, invariants, and scope still
apply. `DesktopProfile`:

- `SAFE` — observations only; all actuation is DENY.
- `ASSISTED` (default) — observations AUTO; every actuation requires
  approval.
- `AUTONOMOUS` — observations AUTO; actuation at or below the
  `autonomous_risk_ceiling` (NONE or LOW — never MEDIUM+) is AUTO inside a
  granted task scope; everything else needs approval.
- `CUSTOM` — a named base mode plus per-action overrides that may only
  *tighten* (AUTO → APPROVAL → DENY). Construction rejects any override
  that loosens the base mode, and invariants are untouched.

Cockpit sessions map onto profiles tighten-only: `safe`/`locked` → SAFE,
`assisted` (default) → ASSISTED, `autonomous` → AUTONOMOUS.

## Approval flow

Actuation that needs approval files an `ApprovalRequest` in the same A33
`ApprovalStore` used everywhere else. Cockpit desktop requests execute with
agent identity `forge-desktop` so a human session actor can decide them
(the store enforces approver ≠ agent, single transition, expiry). Approving
mints a **single-use, scope-bound token**; the resubmitted action carries
`approval_id`. A stale, spent, out-of-scope, or revoked token fails closed
and files a fresh, decidable request. Desktop approvals are session-bound:
a cockpit session sees exactly the requests filed for its id/active task.

## Provider protocol and the fake desktop

`DesktopProvider` is a Protocol with 16 methods (`screenshot`,
`read_screen`, `list_windows`, `window`, `mouse_move`, `mouse_click`,
`keyboard`, `launch`, `file_select`, `file_access`, `clipboard`,
`app_action`, `list_processes`, `process`, `system_info`, `snapshot`).
Providers signal failure with `DesktopProviderError(kind, message)` —
kinds `disconnected`/`unavailable`/`timeout` are marked recoverable; the
agent never fabricates a generic failure over a specific one, and any
contract breach fails closed as `provider_error` without leaking internals.

`FakeDesktopProvider` is a deterministic, scriptable in-memory desktop:
windows, a focused window, a raster-free screen, processes, an apps
registry, seed files, an input event log, and fault injection
(`disconnect`, `reconnect`, `fail_next`, `fail_all`). Its snapshot reports
`simulation: true` and never includes clipboard content. This is the
desktop used by tests, dev, and the sandbox cockpit — and it is **labeled
as a simulation everywhere** (capability endpoint, cockpit, docs). A35
deliberately ships no real-OS provider; the protocol is the plugin point
for one, and nothing in the codebase claims otherwise.

## Bridge boundary

`DesktopBridge` is the authenticated boundary between the Forge server and
the provider: `open_session(actor, project_id, scopes, ttl)` mints a bridge
session; `submit()` re-identifies every request as the session actor and
runs the full agent pipeline; `snapshot()` returns provider state (never
secrets); `simulate()` reconfigures the fake provider only — it refuses on
any provider that is not the fake. Cockpit sessions attach as
`cockpit:{session.id}`. In a deployed split architecture this module is the
natural place for the authenticated wire boundary between server and
desktop host.

## Control plane + API

| Method | Effect |
|---|---|
| `GET /api/v1/desktop/capabilities` | Honest matrix: 15 kinds with risk, profile verdict, executable/observation flags, provider label, `status: "simulation"` |
| `GET /api/v1/desktop/state` | Observation snapshot (screenshot, active window, windows, processes, system) through the pipeline |
| `POST /api/v1/desktop/check` | Full evaluation, never executes |
| `POST /api/v1/desktop/act` | Full pipeline; returns decision/risk/reasons/approval id or execution result (rate-limited) |
| `POST /api/v1/desktop/grants` | Grant task scopes (`*`, `screen`, `windows`, `input`, `clipboard`, `files`, `processes`, `system`, `app:*`, `window:*`, `process:*`, `file:*`) |
| `GET /api/v1/desktop/approvals` | Session-visible pending desktop approvals |
| `POST /api/v1/desktop/approvals/{id}/approve` | Decide + mint single-use token (rate-limited) |
| `POST /api/v1/desktop/approvals/{id}/deny` | Decide deny (rate-limited) |

`desktop_act` builds the request with `session_id` bound to the cockpit
session, `task_id` defaulting to the session's active task; grants only
scope *attempts* — policy, risk, profile, and approval still gate every
action. Schema bounds: action ≤ 64, target/reason ≤ 500, task/approval ids
≤ 128, params a plain dict. All endpoints require an authenticated session;
mutations additionally require the CSRF header.

## Cockpit

The Desktop view (`#/desktop`) shows the session/desktop profile, provider
health + simulation label, live observation state, the capability matrix
(repo-standard `table.data-table` markup), an action composer (WHAT/WHY,
JSON params), and pending approval cards. Approve mints the token, the form
carries it as `approval_id` on resubmission, and a successful execution
consumes it. The view obeys every A34 UI contract: same-origin `api()`
only, no storage, no inline handlers/styles, CSP-clean, navigation-only
palette ("Go to Desktop").

## Security properties (pinned by tests)

- Fail closed: no policy → nothing executes; missing agent identity → DENY.
- Invariants beat policy ALLOW, approval tokens, and AUTONOMOUS profiles.
- CUSTOM profiles cannot loosen, cannot touch invariants.
- Tokens: single-use, scope/operation-bound, expiring; failed redemptions
  fail closed into a fresh approval.
- Task scope grants are explicit, TTL-bounded, and never forgeable by
  request targets.
- Observations are redacted; the fake desktop snapshot never leaks
  clipboard content; provider errors are structured and internals-free.
- The cockpit never misrepresents the simulation as real control.

## Testing

- `tests/test_a35_actions.py` — vocabulary, policy-op mapping, validation
  bounds (bad requests rejected, good accepted), dict round-trips.
- `tests/test_a35_provider.py` — fake desktop state machine, injection,
  snapshot redaction.
- `tests/test_a35_risk.py` — base-risk ordering, raises, fail-safe rank,
  all invariant families, dedup.
- `tests/test_a35_profiles.py` — four modes, ceilings, tighten-only
  overrides, session mapping.
- `tests/test_a35_agent.py` — pipeline order, deny paths, approval
  round-trip, token discipline, grants, audit trail, failure structure.
- `tests/test_a35_security.py` — no-bypass invariants, bridge boundaries,
  redaction, API bounds.
- `tests/test_a35_ui.py` — desktop view contracts + live endpoints.
- `tests/test_a35_e2e.py` — observe → plan → approve → act → verify loops,
  denial leaves state untouched, disconnect/reconnect recovery.
- `tests/test_a35_control_plane.py` — API integration incl. cross-session
  isolation and single-use tokens.

A35 result: **126 tests in the A35 suites; full suite 1011 passed,
2 skipped.**

## What A35 deliberately does not do

Real operating-system control. The provider protocol is the plugin point;
a real provider must implement the same 16-method surface and the same
error contract, and even then the A33 gates, invariants, profiles, and
approvals apply unchanged. A40 (Computer Use) builds the observe→plan→act
loop on top of this layer; A35 provides controlled execution, not an
autonomous computer-use loop.
