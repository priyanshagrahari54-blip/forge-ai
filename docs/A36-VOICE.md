# A36 — Voice (the permission-gated spoken loop)

A36 adds the audio layer around the A33 voice foundation. The A33 layer
(`forge/voice/base.py`, formerly `forge/voice.py` — moved verbatim, the
public names are unchanged) parses voice commands into intents and routes
them through the A33 permission system. A36 wraps it in the complete
spoken loop:

```text
AUDIO (WAV, bounded) / TEXT INPUT
 ↓ 1. wake gate          no wake word → nothing happens (fail closed)
 ↓ 2. transcription      simulated codec only; real audio refused
 ↓ 3. VoiceCommand → intent (A33 templates, unchanged)
 ↓ 4. A33 policy + approval   Resource.VOICE command; approver ≠ agent
 ↓ 5. action             real task creation / informational reply
 ↓ 6. spoken reply       deterministic simulated speech (WAV)
 ↓ 7. audit              permission evaluations + loop decisions
```

Voice is just another request source. Nothing executes outside the same
policy/approval path every other surface uses.

## What A36 adds

| Area | Location | Notes |
|---|---|---|
| Audio primitives | `forge/voice/audio.py` | Bounded 16 kHz mono PCM, strict WAV read/write, RMS levels |
| Simulated speech codec | `forge/voice/codec.py` | Deterministic text⇄tone transport (data codec, honestly labeled) |
| Speech-to-text | `forge/voice/transcriber.py` | Provider protocol + simulated recognizer; structured failures |
| Text-to-speech | `forge/voice/synthesizer.py` | Provider protocol + simulated synthesizer; structured failures |
| Wake word | `forge/voice/wake.py` | Provider protocol + simulated wake marker; gate before any processing |
| Voice session | `forge/voice/session.py` | The full loop composed around the A33 `VoiceInterface` |
| Control plane | `forge/control/control_plane.py` | capabilities / synthesize / transcribe / process / approvals |
| API | `forge/api/routes_commands.py`, `schemas.py` | `/api/v1/voice/*`, rate-limited, session-bound |
| Cockpit | `forge/cockpit/web/` | Voice view: stack, spoken commands, audio round trip, approvals |
| Tests | `tests/test_a36_*.py` | 53 tests across 8 suites |

## Honesty invariants (pinned by tests)

- **Simulation is labeled everywhere.** Capability reports, engine names
  (`forge-simulated-stt/tts/wake`), result payloads, the cockpit, and the
  spoken reply all carry `simulation: true`. The simulated recognizer
  decodes only audio produced by the deterministic simulated codec —
  arbitrary audio (real speech, noise) is refused with a structured
  `unrecognized` error instead of a fabricated transcription. A36 claims
  no real speech recognition or synthesis anywhere.
- **The simulated transport is a data codec over PCM.** `encode_text`
  turns bounded UTF-8 text into tones; `decode_text` decodes exactly
  that framing (preamble, length, checksum, trailer) and fails closed on
  anything else. It is deterministic — the same text always produces the
  same audio.
- **Wake first.** Audio input without the wake marker never reaches the
  command pipeline (`no_wake_word`). The wake gate is explicit; bypassing
  it requires an explicit `require_wake=False` flag, and the result trace
  records that.
- **Real providers plug in, never fabricate.** `SpeechToTextProvider`,
  `TextToSpeechProvider`, and `WakeWordDetector` protocols plus
  `Unconfigured*` stand-ins (`not_configured` failures) are the plugin
  points; env knobs (`FORGE_VOICE_STT_PROVIDER` /
  `FORGE_VOICE_TTS_PROVIDER`) accept only `simulated` in A36 and refuse
  unknown names at startup.

## The voice loop

`VoiceSession.process(speech, agent=…, task_id=…, task_factory=…,
approval_token_id=…)`:

1. **Wake** (audio only; typed text is an explicit user action, recorded
   as `input=text` and never claimed as recognition).
2. **Transcribe** — failures are structured (`unrecognized`,
   `not_configured`) and stop the loop.
3. **Authorize** — the A33 `VoiceInterface.handle()` path: parse into an
   intent, evaluate `Resource.VOICE command` against the permission
   policy, record the evaluation in the audit log, file an approval when
   required, and redeem a supplied single-use token. Voice commands
   execute with agent identity `forge-voice` so a human session actor can
   decide them (the A33 store enforces approver ≠ agent).
4. **Act** — the control plane's task factory maps intents onto real
   actions: `run_tests`, `commit`, `update_website`, `summarize`,
   `review` create tasks through `submit_task` (fully policy-gated);
   `status` produces an informational spoken reply from live task
   counts; anything else is refused. Unknown intents fail closed.
5. **Reply** — every outcome (success, denial, clarification) is spoken
   back through the synthesizer.

## Control plane + API

| Method | Effect |
|---|---|
| `GET /api/v1/voice/capabilities` | Honest stack report: simulation status, provider names, wake word, note |
| `POST /api/v1/voice/synthesize` | Text (≤2000) → WAV in base64 (`audio/wav`), engine + simulation labels (rate-limited) |
| `POST /api/v1/voice/transcribe` | Bounded WAV → transcription; garbage → 400, real audio → 503 `VOICE_UNAVAILABLE` (rate-limited) |
| `POST /api/v1/voice/process` | The full loop: text or audio, approval token, wake flag; returns the complete stage trace + spoken reply (rate-limited) |
| `GET /api/v1/voice/approvals` | Session-visible pending voice approvals |
| `POST /api/v1/voice/approvals/{id}/approve` | Decide + mint a single-use token (rate-limited) |
| `POST /api/v1/voice/approvals/{id}/deny` | Decide deny (rate-limited) |

Voice approvals are session-bound (the request binds to the active task
or the session id, mirroring A35 desktop); the generic run-bound
approvals view does not list them. Tokens are single-use and
scope-bound; a stale or spent token fails closed into a fresh,
decidable approval. Audio is bounded at 500 KB / 60 s, WAV only, and the
CSP is extended by exactly one directive (`media-src 'self' blob:`) so
the cockpit can play spoken replies without widening script/style
authority.

## Cockpit

The Voice view (`#/voice`) shows the voice stack (labeled simulation),
accepts spoken commands as text through the loop, offers an audio round
trip (synthesize → play → send the audio through wake + recognition),
renders the full result trace with a playable spoken reply, and lists
pending voice approvals with approve/deny — carrying the minted token
into the resubmission, exactly like the desktop view. All A34/A35 UI
contracts are preserved: same-origin `api()` only, no storage, no
credentials, CSP-clean, navigation-only palette ("Go to Voice").

## Testing

- `tests/test_a36_audio.py` — WAV round trips, format/bounds validation,
  energy levels.
- `tests/test_a36_codec.py` — deterministic round trips, framing and
  checksum failures, wake markers.
- `tests/test_a36_transcriber.py` / `synthesizer.py` / `wake.py` —
  simulated providers, refusals, `not_configured` honesty.
- `tests/test_a36_session.py` — the full loop: allow/approval/deny,
  single-use tokens, wake gating, unknown intents, real-audio refusal,
  audit.
- `tests/test_a36_control_plane.py` — API integration: capabilities,
  synthesize/transcribe round trip, process with approval round trip and
  token single-use, deny, cross-session isolation, 401/400/503
  boundaries, audit.
- `tests/test_a36_ui.py` — cockpit voice view contracts, CSP, live
  endpoints.

A36 result: **53 new tests; full suite 1075 passed, 2 skipped.**

## Real Providers

A36 now includes real STT/TTS providers alongside the simulated ones:

| Provider | Location | Requirements |
|---|---|---|
| OpenAI Whisper (STT) | `forge/voice/openai_stt.py` | `OPENAI_API_KEY` |
| OpenAI TTS | `forge/voice/openai_tts.py` | `OPENAI_API_KEY` |

Configure via environment variables:
- `FORGE_VOICE_STT_PROVIDER=openai-whisper` — real speech-to-text
- `FORGE_VOICE_TTS_PROVIDER=openai-tts` — real text-to-speech
- `FORGE_WHISPER_MODEL` — Whisper model (default: `whisper-1`)
- `FORGE_TTS_MODEL` — TTS model (default: `tts-1`)
- `FORGE_TTS_VOICE` — TTS voice (default: `alloy`)

Real providers are labeled `simulation=false` and use the real API.
The same security model applies: voice remains a permission-gated
request source, and all existing pipeline tests continue to pass.

## What A36 deliberately does not do

Browser microphone capture is not part of the A36 cockpit: the
browser records non-WAV container formats and converting them needs
a real recognizer — the view says so and offers the full simulated
audio round trip instead.
