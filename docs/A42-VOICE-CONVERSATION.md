# A42 — Natural Voice Conversation

A42 layers multi-turn conversation over the A36 voice gate: context,
barge-in, clarifying questions, confirm-before-execute, and spoken
results — without touching the one-shot voice loop.

## What A42 adds

`forge/voice/conversation.py` — `VoiceConversation`:

- **Short-term context**: the previous turn's target fills pronoun
  references ("it", "that", "this one") deterministically, so
  follow-ups attach to the right object. Bounded, documented
  heuristic.
- **Barge-in**: `interrupt()` marks the active turn `interrupted` and
  blocks any action from it (no task, no reply); the next utterance
  starts fresh.
- **Clarifying questions**: unrecognized speech is never guessed at —
  the engine asks a spoken question and waits (`awaiting_answer`).
- **Confirm before executing**: task-creating intents pause at
  `awaiting_confirmation`; only an affirmative reply acts — and even
  then only through the existing A36 `VOICE/command` permission gate
  (approval round trips unchanged). Negatives cancel; ambiguous
  answers re-ask.
- **Spoken results**: every assistant turn carries a spoken summary
  (including live status replies via the A36 task factory).
- **Bounded**: max 24 turns per conversation, 4 conversations per
  session.

Control plane: `voice_conversation_start/say/interrupt/state`
(session-scoped, isolated, audited). API:
`POST /api/v1/voice/conversations`, `.../{id}/say`,
`.../{id}/interrupt`, `GET .../{id}` — text or WAV audio turns; 404
outside the owning session; 400 on mixed/empty input; 409 over the
conversation cap.

## Security notes

- Conversation turns go through the exact same voice permission path
  as A36 — conversations can never bypass policy or approvals.
- Interruption is enforced before execution: a barged-in turn cannot
  create tasks.
- Confirmation is an additional, conversation-level gate; it never
  replaces the A33 gate (defense in depth).

## Testing

`tests/test_a42_conversation.py` (12): context resolution,
clarification, confirm/no-op, affirmative executes through the gate,
negative cancels, ambiguous re-ask, barge-in blocks actions and
recovers, bounded turns, reply-only intents, plane lifecycle (real
task creation), interrupt + cross-session isolation, caps and
validation. `tests/test_a42_api.py` (3): HTTP flow, boundaries,
isolation.

A42 result: **15 new tests; full suite 1216 passed, 2 skipped** (A41
baseline: 1201 passed, 2 skipped).
