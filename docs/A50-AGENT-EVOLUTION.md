# A50 — Agent Evolution

Agents evolve from evidence, not stories: real terminal run outcomes
(status, attempts, elapsed time) are recorded into a per-agent ledger
that drives generation counters and honest metrics.

## What A50 adds

- `forge/agents/evolution.py` — `AgentEvolution` ledger per session:
  recording one real terminal run bumps the agent's generation
  exactly once and computes `runs`, `succeeded`, `failed`,
  `success_rate`, `avg_attempts`, `avg_elapsed_ms`, `last_outcome`
  from the recorded rows. No fake learning: an agent with no
  recorded outcomes has no metrics.
- `AgentDefinition` gains `generation` (starts at 1) and `metrics`.
- Control plane `agent_record_outcome()` — only *terminal* runs may
  be recorded (running/paused/queued are refused honestly), unknown
  agents/tasks are refused, and every recording is audited under
  `agents/evolve`.
- API: `POST /api/v1/agents/{name}/outcomes`,
  `GET /api/v1/agents/{name}/evolution`.

## Security notes

- Evolution is read-mostly bookkeeping: it grants nothing, changes no
  permissions, and cannot influence policy.

## Testing

`tests/test_a50_agent_evolution.py` (6): unrecorded defaults,
real-terminal recording with computed metrics, refusal of unfinished
runs, unknown-target refusal, API flow, and metric arithmetic.

A50 result: **6 new tests; full suite 1276 passed, 2 skipped** (A49
baseline: 1270 passed, 2 skipped).
