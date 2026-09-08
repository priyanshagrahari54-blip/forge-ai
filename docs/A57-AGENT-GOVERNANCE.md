# A57 — Agent Governance

Runtime quotas for agents, enforced where runs are dispatched.

## What A57 adds

- `forge/agents/governance.py` — `AgentGovernor`: per-agent limits
  (default 60 runs/hour, 2 concurrent; bounds 1–1000/hour, 1–20
  concurrent) with a sliding one-hour start window and an active-run
  counter.
- Enforcement: `agent_run` checks quotas **before any work starts**
  (before the policy gate, before dispatch), so a quota violation
  has no side effects; refusals are audited and surfaced honestly.
  Team members share the same quotas because teams dispatch through
  `agent_run`.
- Control plane `agent_set_limits` / `agent_limits` (audited); API
  `PUT/GET /api/v1/agents/{name}/limits`.

## Security notes

- Limits can only restrict — they can never grant anything. The
  governor is an additional gate stacked on lifecycle and the
  AGENT/execute policy gate.

## Testing

`tests/test_a57_agent_governance.py` (5): defaults + validation
bounds, hourly limit enforced before dispatch, concurrency limit
enforced while a run is in flight (with a blocking provider),
team members sharing the quota, API flow.

A57 result: **5 new tests; full suite 1313 passed, 2 skipped** (A56
baseline: 1308 passed, 2 skipped).
