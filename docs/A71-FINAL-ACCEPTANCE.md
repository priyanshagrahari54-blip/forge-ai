# A71 — Final Acceptance

A bounded end-to-end acceptance checklist.

## What A71 adds

- `forge/final/acceptance.py` — six live checks: registered
  projects, fabric model inventory, permission policy presence,
  database liveness, the agent gate surface, and a real smoke run.
  The smoke submits an actual task through the real pipeline in
  autonomous mode; when the run reaches the approval gate, the
  harness acts as the operator through the real approval store
  (single-use tokens, audited) and records every approval it
  drove. Timeouts and failures are reported as failed checks with
  the real error — nothing is pre-marked passed.
- Control plane `final_acceptance` (audited); API
  `POST /api/v1/final/acceptance`.

## Security notes

- Approval driving uses the exact approval machinery operators
  use; acceptance can never bypass PolicyGate or ChangeSet.

## Testing

`tests/test_a71_final_acceptance.py` (5): full pass on a working
plane (including a genuinely SUCCEEDED smoke run), honest failure
without a policy (approval-gate-only smoke is reported
accurately), a real smoke failure when the project is broken,
stable checklist structure, API flow.

A71 result: **5 new tests; full suite 1384 passed, 2 skipped** (A70
baseline: 1379 passed, 2 skipped).
