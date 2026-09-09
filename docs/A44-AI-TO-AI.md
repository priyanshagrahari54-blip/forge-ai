# A44 — AI-to-AI Collaboration

A44 lets a Forge agent consult an external AI and continue working —
with the external contribution always marked and always untrusted.

## What A44 adds

- `forge/collaboration/connectors.py` — `ExternalAIConnector`
  protocol, `SimulatedExternalAIConnector` (deterministic, bounded,
  no network, labeled `simulation: true`), `OpenAIConnector` (real
  OpenAI API calls, requires `OPENAI_API_KEY`, labeled
  `simulation: false`), fail-closed `build_connector`, and a bounded
  per-session `CollaborationSession` log.
- **Real OpenAI connector** — `forge/collaboration/openai_connector.py`:
  real ChatCompletion calls via the OpenAI API. Requires
  `OPENAI_API_KEY`; returns `available()=False` when unset. Response
  is bounded to 6000 chars, latency measured, labeled `simulation=false`.
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

## Configuration

| Variable | Effect |
|---|---|
| `OPENAI_API_KEY` | Enables the real `openai` connector |
| `FORGE_OPENAI_MODEL` | Model for OpenAI connector (default: `gpt-4o-mini`) |
| `OPENAI_BASE_URL` | Custom API base URL (default: OpenAI) |

## Security notes

- External text is untrusted input by construction — the same status
  as model output and image content. It can never authorize actions.
- Real connector responses carry `simulation=false` and the real
  model name; simulated responses carry `simulation=true`.
- API keys are read from environment variables, never stored.

## Testing

`tests/test_a44_collaboration.py` (6): connector honesty + bounds +
fail-closed resolution, ALLOW/DENY gating with untrusted marking,
approval round trip with token replay refiling, cross-session
isolation, API boundaries.

A44 result: **6 new tests; full suite 1232 passed, 2 skipped** (A43
baseline: 1226 passed, 2 skipped).
