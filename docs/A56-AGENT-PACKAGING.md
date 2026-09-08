# A56 — Agent Packaging

Agent definitions become portable, validated artifacts.

## What A56 adds

- `forge/agents/packaging.py` — exports are plain JSON
  specifications (`forge-agent-definition` format v1): name, role,
  capabilities, base capabilities, skills, description, generation,
  metrics, provenance. No secrets, no executors, ever.
- Imports re-run the full factory validation (name/role regexes,
  canonical capability vocabulary, size bounds ≤ 64 KB) and always
  arrive **unbound** — an imported definition has no executor until
  someone binds one locally, so import cannot smuggle execution
  power. Skills unknown to the target session are dropped and
  reported honestly; capabilities recompute from base + surviving
  skills.
- Control plane `agent_export` / `agent_import` (audited); API
  `GET /api/v1/agents/{name}/export`,
  `POST /api/v1/agents/import`.

## Security notes

- Import = data ingestion through the same validators as local
  creation; provenance is preserved (imported_by prefix) and real
  bindings are never portable.

## Testing

`tests/test_a56_agent_packaging.py` (5): plain-spec exports with no
secret/executor fields, roundtrip import arrives unbound, garbage
payload refusals, honest unknown-skill dropping, API flow.

A56 result: **5 new tests; full suite 1308 passed, 2 skipped** (A55
baseline: 1303 passed, 2 skipped).
