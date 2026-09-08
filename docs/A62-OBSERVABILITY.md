# A62 — Observability

Honest plane metrics computed from real pipeline events.

## What A62 adds

- `forge/observability/metrics.py` — thread-safe counters plus
  bounded duration reservoirs (most recent 100 samples) with
  mean/p50/p95/max summaries.
- Counters recorded at the existing funnels — guarded so metrics
  can never break a run:
  - `tasks.submitted` (submit_task), `runs.succeeded` /
    `runs.failed` and `run.duration_ms` (both run-outcome paths),
    `agent_runs.succeeded` / `agent_runs.failed` and
    `agent_run.duration_ms` (agent-run worker).
- Control plane `observability_snapshot` with real gauges: active
  sessions, defined agents, teams, non-terminal runs, and failure-
  ledger totals. API `GET /api/v1/observability/metrics`.

## Security notes

- Counters live for the plane lifetime (no persistence claim);
  snapshots contain only aggregate numbers — never prompts, file
  contents, or user data.

## Testing

`tests/test_a62_observability.py` (5): counters and duration
reservoirs from a real successful task run, honest failure
counting, gauges reflecting plane state, snapshot stability, API
flow.

A62 result: **5 new tests; full suite 1338 passed, 2 skipped** (A61
baseline: 1333 passed, 2 skipped).
