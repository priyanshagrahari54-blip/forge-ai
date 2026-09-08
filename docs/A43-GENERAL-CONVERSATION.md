# A43 — General Conversation Engine

A43 gives Forge a single conversational front door: every message is
classified, routed, and answered with real information — or turned
into a real task. Nothing is fabricated.

## Routing (deterministic, documented)

| Kind | Trigger | Action |
|---|---|---|
| `task_request` | actionable engineering verbs (build/add/fix/implement/analyze/…) | `submit_task` into the existing supervisor pipeline; reply carries the real task id + requirement |
| `question` | interrogatives, "explain/describe/summarize/…", or "?" | answered with real data: RepositoryIntelligence (files/packages/entry points), live dashboard task counts, remembered facts — or an honest "I don't have a real answer" fallback |
| `preference` | "remember that…", "I prefer…", "always…" | stored through the normal (policy-gated) A37 memory path; refusal is reported honestly |
| `greeting` / `chat` | greetings / anything else | deterministic bounded replies that state exactly what Forge can do |

## What A43 adds

- `forge/conversation/engine.py` — classification + routing + bounded
  history (16 messages), with memory callbacks that never break
  replies.
- Control plane: `converse(session, message)` and
  `conversation_history(session)` — real answers via
  `RepositoryIntelligence`, real task creation via `submit_task`,
  memory through the existing gated `memory_add` (DENY → honest
  refusal; approval rules unchanged).
- API: `POST /api/v1/conversation` (rate-limited), `GET
  /api/v1/conversation`; 400 on empty/oversized messages.
- Cockpit: Conversation view (chat log + input) with the usual
  contracts (renderer calls only `/api/v1/conversation`, no storage,
  no inline handlers), palette entry, nav link.

## Security notes

- Task routing inherits every supervisor/policy/approval guarantee —
  conversation is an entry point, not a bypass.
- Question answering only reads real data (repository intelligence,
  dashboard, session memory); it never invents facts.
- Memory writes go through the A37 `MEMORY/write` gate; denials are
  surfaced, never silently ignored.

## Testing

`tests/test_a43_conversation.py` (10): deterministic classification
(incl. the "how do you build X?" question-vs-task edge), task routing,
preference remember/refuse, question routing without fabrication,
bounded history, plane answers from real repository + dashboard data,
real task creation, persisted preferences, API boundaries, cockpit
contracts.

A43 result: **10 new tests; full suite 1226 passed, 2 skipped** (A42
baseline: 1216 passed, 2 skipped).
