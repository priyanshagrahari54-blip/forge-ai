# A72 — Final Verification

Evidence checks on finished runs.

## What A72 adds

- `forge/final/verification.py` — verifies a run's recorded
  evidence without re-executing anything: terminal status,
  success, a recorded report, and every listed output file
  existing on disk. Unknown or cross-project runs report
  NOT_FOUND (no existence leak); pending runs verify false with
  the honest reason; failures surface the real error.
- Control plane `final_verify_run` (audited); API
  `POST /api/v1/final/verify-run`.

## Security notes

- Read-only evidence inspection; verification can never modify
  runs or files.

## Testing

`tests/test_a72_final_verification.py` (5): successful run with
all evidence checks passing, honest failed-run verification,
NOT_FOUND isolation, pending-run reporting, API flow.

A72 result: **5 new tests; full suite 1389 passed, 2 skipped** (A71
baseline: 1384 passed, 2 skipped).
