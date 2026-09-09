# A70 — UX Polish

Dashboard panels that show real, live state.

## What A70 adds

- Dashboard autonomy strip: the effective autonomy level plus
  per-operation counts (autonomous / approval / blocked) straight
  from `GET /api/v1/autonomy`, alongside pipeline counters and
  gauges (tasks submitted, runs ok/failed, agents, active runs)
  from `GET /api/v1/observability/metrics`.
- Dashboard security audit panel: overall posture, policy rule
  count + findings, active sessions, and secret-pattern hits from
  `GET /api/v1/hardening/report` — read-only, no state changes.
- Polish primitives: `fmtCount` compact numbers, chip styles,
  audit lines, aria labels; every panel degrades honestly
  ("backend offline") instead of faking data, and the cockpit
  keeps its class-toggling-only invariant.

## Security notes

- All three feeds are authenticated reads of aggregate data —
  no new write surface, no secrets displayed.

## Testing

`tests/test_a70_ux_polish.py` (5): the three feed endpoints
return the exact shapes the panels render, dashboard template
containers exist, the JS wires the endpoints with graceful
failure paths, styles cover the new components, and the panels
track live state changes (autonomy transition + counters after
real work).

A70 result: **5 new tests; full suite 1379 passed, 2 skipped** (A69
baseline: 1374 passed, 2 skipped).
