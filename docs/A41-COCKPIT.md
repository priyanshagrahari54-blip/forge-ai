# A41 — Premium Browser Cockpit

A41 completes the cockpit surface: an Agents catalog, a Security
posture page, and Settings, plus theme support and responsive layout —
on top of the existing palette (Ctrl+K) and views.

## Views (all live over real endpoints)

| View | Backing data |
|---|---|
| Overview / Tasks / Task / Projects / Models / Permissions / Git / Activity / Approvals / Desktop / Voice / Memory / Orchestrations / Vision / Computer Use / System | existing cockpit routes |
| Agents | `GET /api/v1/agents` — the documented agent inventory: role, capabilities, and the real A33 gate for each agent; simulated providers labeled |
| Security | `GET /api/v1/security` — mode, policy default, indexed rules, recorded permission evaluations, hard invariants, provider kinds |
| Settings | `GET /api/v1/sessions/me` + theme toggle + shortcut help |

## What A41 adds

- **Agents catalog** (`plane.agent_catalog()`, `/api/v1/agents`):
  architecture metadata only — every row names the resource that
  actually gates the agent, and simulated providers are labeled
  `simulated: true`.
- **Security posture** (`plane.security_overview()`, `/api/v1/security`):
  non-sensitive posture — never credentials, secrets, or raw audit
  content.
- **Theme support**: dark-first design with a complete
  `[data-theme="light"]` variable set; toggled from Settings, applied
  per session (no storage used, by design).
- **Responsive layout**: `@media (max-width: 760px)` collapses the
  sidebar and grids.
- **Command palette** already ships Ctrl+K/Cmd+K (A34); palette
  entries for the new views plus "Toggle theme".

## Security notes

- The new endpoints return only non-sensitive metadata; they are
  authenticated like every other cockpit endpoint.
- The cockpit continues to enforce its own contracts: no inline
  handlers/styles/scripts in templates, no browser storage or raw
  fetch in renderers, renderers only call their own endpoint groups.

## Testing

`tests/test_a41_cockpit.py` (6): catalog endpoint + row/gate honesty,
posture endpoint + simulation labels, view/template/palette contracts,
theme + responsive CSS presence, renderer hygiene (no storage, no
inline), endpoint scoping per renderer.

A41 result: **6 new tests**; suite grows to 1201 with A41 (full
regression run at stage close).
