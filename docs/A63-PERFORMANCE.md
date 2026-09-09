# A63 — Performance

Real run timings, honest bounded aggregates.

## What A63 adds

- `forge/performance/profiler.py` — `run_profile` derives queue,
  execution, and total durations strictly from the run record's own
  timestamps (created/started/finished); nothing is estimated.
  `performance_summary` aggregates the most recent runs (bounded:
  ≤200 considered, ≤5 slowest listed) with mean/median/p95/max
  statistics, per-mode counts, and the slowest runs.
- Control plane `performance_summary` (project-scoped, validated
  limit) and `performance_run` (cross-project ids map to NOT_FOUND,
  so existence never leaks). API
  `GET /api/v1/performance/summary`,
  `GET /api/v1/performance/runs/{run_id}`.

## Security notes

- Read-only observations: profiles expose timings and statuses
  only, never requirements, file contents, or reports.

## Testing

`tests/test_a63_performance.py` (5): profile from real timestamps
(a deliberately slow provider dominates execution time), aggregate
summary over multiple runs, validation + cross-project isolation,
honest pending-run profile, API flow.

A63 result: **5 new tests; full suite 1343 passed, 2 skipped** (A62
baseline: 1338 passed, 2 skipped).
