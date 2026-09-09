# A59 — Failure Learning

A persistent, bounded ledger turns failures into honest lessons.

## What A59 adds

- `forge/learning/failures.py` — `FailureLedger` over the plane
  database: every failure is fingerprinted (bounded 48-char key,
  500-char error) and counted; at most 500 distinct keys, oldest
  evicted. Learning survives restarts.
- Automatic funnels: task failures (both supervisor-outcome
  failures and worker errors) and agent-run failures are recorded
  with no extra wiring; recording can never break the pipeline
  (guarded try/except).
- Control plane `record_failure` (validated categories:
  task/stage/agent/model/system; audited) and `failure_lessons`
  (top fingerprints + derived one-line lessons + stats); API
  `POST /api/v1/learning/failures`,
  `GET /api/v1/learning/lessons`.

## Security notes

- Lessons are bounded summaries of real recorded errors — never
  invented fixes, never other people's data, and nothing in the
  ledger grants or changes any capability.

## Testing

`tests/test_a59_failure_learning.py` (5): task failures learned
automatically from a real failed run, agent failures learned
automatically, deduplication counts + category/error validation,
persistence across plane restarts on the same database, API flow.

A59 result: **5 new tests; full suite 1323 passed, 2 skipped** (A58
baseline: 1318 passed, 2 skipped).
