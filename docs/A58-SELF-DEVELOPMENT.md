# A58 — Agent Self-Development

Agents learn from their own real failures — through validated,
bounded edits only.

## What A58 adds

- Failure feed: worker-level agent-run failures are now recorded in
  the bounded per-agent run log (A51's log, same 20-entry cap), so
  self-development has real evidence to work from.
- `forge/agents/selfdev.py` — `SelfDevLedger`: `analyze` inspects
  the agent's recorded failed runs and returns either a
  `failure-note` proposal (bounded journal text derived from the
  actual error + failure metrics) or `none` with an honest reason
  (no failures recorded, or the failure class is not self-fixable —
  e.g. "without a bound executor").
- `selfdev_apply` applies only `failure-note` proposals and only
  through the existing factory validation: appends a bounded
  `[learned]` note to the definition description, bumps generation,
  merges metrics. Caps stop learning loops: max 8 applied
  proposals per session and 3 per agent.
- Control plane `selfdev_analyze/apply/ledger` (audited under
  `selfdev`); API `POST /api/v1/agents/{name}/selfdev/analyze`,
  `POST .../apply`, `GET /api/v1/agents/{name}/selfdev`.

## Security notes

- Self-development never touches executors, bindings, other
  agents, or policy, and can never invent capabilities. Budgets and
  validation bound every change.

## Testing

`tests/test_a58_self_development.py` (5): no-failure analysis is
honest ("none"), unbound-executor failures are refused as not
self-fixable with nothing changed, failure-note apply goes through
validation (description note + generation bump + metrics),
budget caps stop learning loops at 3 per agent, API flow.

A58 result: **5 new tests; full suite 1318 passed, 2 skipped** (A57
baseline: 1313 passed, 2 skipped).
