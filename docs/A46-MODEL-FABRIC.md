# A46 — Model Fabric (controlled bridge)

The model fabric itself (routing, registry, failover, health,
telemetry) was built in A31 and is fully tested. A46 adds the
*controlled* path: the plane's permission-gated bridge from agent to
model, with honest routing metadata — the fabric becomes a governed
capability of the control plane rather than only an internal
supervisor component.

## What A46 adds

- `forge/models/bridge.py` — `FabricBridge.generate()` wraps a
  fabric-routed generation and returns structured metadata: which
  model/provider answered, `provider_kind`, success/error, latency,
  tokens, and an honest `simulated` flag derived from provider
  registry data (the built-in local provider and mock/fallback kinds
  are marked simulated; the local no-op response says to configure a
  real provider instead of faking generation).
- Control plane `model_generate()` — MODEL/call policy first (with
  the capability as a matching detail), then routing. DENY fails
  closed; REQUIRE_APPROVAL files a session/task-scoped approval and
  only a redeemed single-use token releases the call; spent tokens
  refile. Every call is audited with provider/model/simulated
  metadata.
- `model_approvals()` / `decide_model_approval()` — session-isolated
  approval lifecycle.
- API `/api/v1/models/*`: `POST /models/generate` (rate-limited),
  `GET /models/approvals`, approve/deny.
- Conversation integration: questions like "what models do you have?"
  are answered from the real fabric registry, naming the built-in
  no-op honestly.

## Security notes

- Model calls are permission decisions like every other controlled
  action; approvals are minted for `forge-model` and bound to the
  task, so tokens never leak across sessions.
- Fabric responses are model output — untrusted input by the same
  rules as A44 external AI content.

## Testing

`tests/test_a46_model_fabric.py` (7): no-op local provider marked
simulated, mock vs remote kinds labeled honestly, ALLOW/DENY gating
with audit metadata, approval round trip with spent-token refile,
conversation answers from the real registry, API boundaries.

A46 result: **7 new tests; full suite 1245 passed, 2 skipped** (A45
baseline: 1238 passed, 2 skipped).
