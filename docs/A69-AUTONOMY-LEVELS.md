# A69 — Autonomy Levels

Consultative autonomy, derived from the live policy.

## What A69 adds

- `forge/autonomy/controller.py` — autonomy reports computed from
  the actual permission policy: for every resource/operation pair,
  each matching rule is evaluated with a request aligned to that
  rule's constrained dimensions, and the most permissive outcome
  is reported (autonomous / approval / blocked; no rule = fail
  closed = blocked).
- Level transitions are validated: stepwise increases
  (safe → assisted → autonomous), decreases always allowed,
  duplicates refused, and profiles outside the trio (e.g. locked)
  manage their own autonomy and refuse overrides.
- The level is real, not cosmetic: a session's autonomy override
  becomes the run mode for subsequently submitted tasks — and the
  level can never grant anything the policy denies.
- Control plane `autonomy_report` / `autonomy_set_level`
  (audited); API `GET/POST /api/v1/autonomy`.

## Security notes

- The controller only probes and summarizes; every actual gate
  still runs through PolicyGate/ChangeSet/A33 exactly as before.

## Testing

`tests/test_a69_autonomy.py` (6): report derived from a live
policy (autonomous read / approval write / blocked delete),
stepwise transition validation, safe-profile reports, locked
profiles refuse overrides, the level selecting the real run mode,
controller unit contracts.

A69 result: **6 new tests; full suite 1374 passed, 2 skipped** (A68
baseline: 1368 passed, 2 skipped).
