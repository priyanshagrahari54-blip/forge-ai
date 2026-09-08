# A45 — AI Council

The AI council convenes independent model members, computes a lead
review with honest consensus, and returns an advisory verdict that
disagreements can never be hidden.

## What A45 adds

- `forge/council/engine.py` — `CouncilMember` protocol and
  `SimulatedCouncilModel`s with fixed, distinct stances (every opinion
  labeled `simulation=True` with its member/model names). Real
  providers can plug in behind the same protocol later; this build
  never pretends real models were consulted.
- `AICouncilEngine.deliberate()` — bounded question (1–4000 chars),
  per-member deliberation, honest tally: `stance`, `consensus`,
  `confidence` = agreeing/total (computed from actual opinions, not
  assumed), `disagreements`, verbatim `minority_opinions`, flagged
  final answer.
- **Advisory by design**: the verdict executes nothing, writes
  nothing, and can never authorize an action — it is input, exactly
  like model output.
- Control plane `council_convene/capabilities/history` (session
  bounded to the last 12 deliberations, audited under `council`);
  API `/api/v1/council/*` (capabilities, convene rate-limited,
  history).

## Security notes

- No council path reaches the workspace, approvals, or policy: the
  result is returned to the caller as advisory text only.
- Auditing records every convene with members/stance/confidence in
  the reason.

## Testing

`tests/test_a45_council.py` (6): simulated-label honesty, bounded
inputs, disagreement flagging with preserved minorities, honest
unanimity, empty-council refusal, plane advisory status + audit
records, API boundaries.

A45 result: **6 new tests; full suite 1238 passed, 2 skipped** (A44
baseline: 1232 passed, 2 skipped).
