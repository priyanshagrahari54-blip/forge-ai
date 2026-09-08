# A53 — Agent Memory

Runtime-defined agents get durable, policy-gated memory.

## What A53 adds

- `forge/agents/memory.py` — `AgentMemoryStore` (SQLite table
  `agent_memory`): key/value facts scoped to one agent, bounded
  (64-char keys, 2000-char values, 200 entries per agent).
- Control plane `agent_memory_set/get/list/delete` — every access is
  gated by `Resource.MEMORY` with scope `agent:{name}`; DENY fails
  closed (nothing stored or read silently), writes/deletes are
  audited under `agents`.
- API: `POST /api/v1/agents/{name}/memory`,
  `GET /api/v1/agents/{name}/memory[/{key}]`,
  `DELETE /api/v1/agents/{name}/memory/{key}`.
- Durability: memory survives plane restarts (same database).

## Security notes

- Agent memory inherits the A37 memory permission model exactly:
  read, write, and delete are separate policy decisions; a missing
  rule means denial.

## Testing

`tests/test_a53_agent_memory.py` (5): roundtrip + persistence across
a fresh plane on the same database, DENY fail-closed, value
truncation and unknown-agent refusal, real per-agent entry cap, API
flow.

A53 result: **5 new tests; full suite 1293 passed, 2 skipped** (A52
baseline: 1288 passed, 2 skipped).
