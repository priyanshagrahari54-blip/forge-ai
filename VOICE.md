# FORGE AI — VOICE SPECIFICATION
## Pipeline
Microphone → STT → language/normalization → conversation/action classification → context/memory → policy → response/action → TTS.

## Languages
Hindi, Hinglish and English.

## States
IDLE, LISTENING, PROCESSING, SPEAKING, WAITING_APPROVAL, ERROR.

## Behavior
Normal conversation should answer naturally. Commands should execute directly when policy permits. Consequential actions use approval gates. Do not ask Yes/No for every harmless utterance.

## Reliability
Centralize speakText/TTS routing, prevent microphone feedback, handle interruption and show truthful capability state.

## E2E acceptance
Speech recognized → correct intent → correct action/answer → result returned → spoken response.
