# A55 — Agent Lifecycle

Defined agents have real lifecycle states that gate execution.

## What A55 adds

- `AgentDefinition.status` — new agents start `active`; transitions
  are validated: `active → paused → active`, `active/paused →
  retired`, and `retired` is terminal (no resurrection, no further
  transitions). Unknown states are refused.
- Execution gating: `agent_run` refuses non-active agents with an
  honest explanation; team creation refuses non-active members.
- Control plane `agent_set_status` (audited under `agents/status`);
  API `POST /api/v1/agents/{name}/status`.

## Security notes

- Lifecycle is a control-plane gate on top of the existing
  AGENT/execute policy gate — it can only restrict further, never
  loosen.

## Testing

`tests/test_a55_agent_lifecycle.py` (5): default active, transition
validation and refusals, paused agents refuse runs, teams require
active members, API flow.

A55 result: **5 new tests; full suite 1303 passed, 2 skipped** (A54
baseline: 1298 passed, 2 skipped).
