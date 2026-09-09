# A52 — Agent Teams

Runtime-defined agents compose into ordered teams that execute
sequentially with real handoffs.

## What A52 adds

- `forge/agents/teams.py` — `TeamRegistry` with validated team
  definitions: members must be known, real (bound) agents; bounded
  (max 6 members, 12 teams/session); duplicates refused.
- Control plane `team_create` / `team_execute` / `team_run_result` /
  `team_list` — execution dispatches each member through the full
  A51 `agent_run` path, so every member inherits the AGENT/execute
  gate (DENY fail-closed, approval round trips) and runs as its own
  real recorded run. Steps are strictly sequential; each member's
  bounded output summary is handed to the next member as context.
  The team run is itself a real recorded Run (`team-run-*`) whose
  report carries every member result — failures are never hidden.
- API: `POST/GET /api/v1/teams`, `POST /api/v1/teams/{id}/execute`,
  `GET /api/v1/teams/{id}/result/{run_id}`.

## Security notes

- Teams are composition, not a new permission path: each member's
  execution is gated exactly like a direct agent run, and the team
  record is audited under `teams`.

## Testing

`tests/test_a52_agent_teams.py` (5): member validation (unknown,
unbound, empty, oversized, duplicates), sequential real results with
handoff, DENY fail-closed, session isolation, API flow.

A52 result: **5 new tests; full suite 1288 passed, 2 skipped** (A51
baseline: 1283 passed, 2 skipped).
