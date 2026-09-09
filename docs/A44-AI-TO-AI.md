# A44 — AI-to-AI Collaboration

A44 lets a Forge agent consult an external AI and continue working —
with the external contribution always marked and always untrusted.

## What A44 adds

- `forge/collaboration/connectors.py` — `ExternalAIConnector`
  protocol, `SimulatedExternalAIConnector` (the only connector in
  A44: deterministic, bounded, no network, labeled
  `simulation: true`), fail-closed `build_connector`, and a bounded
  per-session `CollaborationSession` log.
- **Every external response is marked**: `source="external_ai"`,
  `untrusted=true`, explicit connector/model labels, honest
  simulation flag, bounded content. The module executes nothing,
  writes nothing, grants nothing — an external answer can only become
  context the calling agent evaluates.
- **Every consultation is permission-gated**: a `Resource.MODEL /
  call` decision with the connector name as provider detail. DENY
  fails closed; REQUIRE_APPROVAL files an approval and only a
  redeemed single-use token releases the call; spent tokens refile a
  fresh approval.
- Control plane: `collaboration_consult/capabilities/history`,
  approvals (session-scoped, isolated); API `/api/v1/ai-to-ai/*`
  (capabilities, consult rate-limited, history, approvals,
  approve/deny); audited under the `ai-to-ai` category.

## Security notes

- External text is untrusted input by construction — the same status
  as model output and image content. It can never authorize actions.
- No real network exists in this build; the simulated connector says
  so in every response and in `/capabilities`.

## Testing

`tests/test_a44_collaboration.py` (6): connector honesty + bounds +
fail-closed resolution, ALLOW/DENY gating with untrusted marking,
approval round trip with token replay refiling, cross-session
isolation, API boundaries.

A44 result: **6 new tests; full suite 1232 passed, 2 skipped** (A43
baseline: 1226 passed, 2 skipped).
