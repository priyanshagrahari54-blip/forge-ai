"""Voice session orchestration (A36).

One end-to-end voice loop with the A33 permission system in the middle:

    audio → wake gate → transcription → VoiceCommand → intent
    → A33 policy + approval (VoiceInterface) → action → spoken reply
    → audit

Voice is just another request source. Nothing here executes without the
same policy/approval path every other surface uses, and the loop fails
closed at every stage (no wake → no recognition → no action; unknown
intent → no action; denied permission → no action; no approval → no
action).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from forge.voice.audio import AudioChunk
from forge.voice.base import VoiceCommand, VoiceInterface, VoiceIntent
from forge.voice.synthesizer import (SimulatedSpeechSynthesizer, SynthesisError,
                                      TextToSpeechProvider)
from forge.voice.transcriber import (SimulatedSpeechToText,
                                      SpeechToTextProvider, Transcription,
                                      TranscriptionError)
from forge.voice.wake import (SimulatedWakeWordDetector, WakeWordDetector,
                               WakeWordError)


@dataclass
class VoiceLoopResult:
    """Full trace of one voice interaction (never raw audio in dicts)."""

    ok: bool
    simulation: bool
    stages: list[dict[str, Any]] = field(default_factory=list)
    transcription: dict[str, Any] | None = None
    intent: dict[str, Any] | None = None
    permission: dict[str, Any] | None = None
    action: dict[str, Any] | None = None
    reply_text: str = ""
    response_audio: AudioChunk | None = None
    response_engine: str = ""
    response_simulation: bool = False
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "ok": self.ok,
            "simulation": self.simulation,
            "stages": list(self.stages),
            "transcription": self.transcription,
            "intent": self.intent,
            "permission": self.permission,
            "action": self.action,
            "reply_text": self.reply_text,
            "error": self.error,
        }
        if self.response_audio is not None:
            payload["response_audio"] = {
                "engine": self.response_engine,
                "simulation": self.response_simulation,
                "duration_ms": self.response_audio.duration_ms,
                "bytes": len(self.response_audio.data),
            }
        return payload


class VoiceSession:
    """Run the complete voice loop with the A33 permission system."""

    def __init__(self, voice: VoiceInterface, *,
                 transcriber: SpeechToTextProvider | None = None,
                 synthesizer: TextToSpeechProvider | None = None,
                 wake: WakeWordDetector | None = None) -> None:
        self.voice = voice
        self.transcriber = transcriber or SimulatedSpeechToText()
        self.synthesizer = synthesizer or SimulatedSpeechSynthesizer()
        self.wake = wake or SimulatedWakeWordDetector()

    @property
    def simulation(self) -> bool:
        return bool(getattr(self.transcriber, "simulation", False)
                    and getattr(self.synthesizer, "simulation", False)
                    and getattr(self.wake, "simulation", False))

    def capabilities(self) -> dict[str, Any]:
        return {
            "simulation": self.simulation,
            "transcriber": {"name": self.transcriber.name,
                            "simulation": bool(getattr(
                                self.transcriber, "simulation", False)),
                            "available": self.transcriber.available()},
            "synthesizer": {"name": self.synthesizer.name,
                            "simulation": bool(getattr(
                                self.synthesizer, "simulation", False)),
                            "available": self.synthesizer.available()},
            "wake": {"name": self.wake.name, "word": getattr(
                self.wake, "word", ""),
                "simulation": bool(getattr(self.wake, "simulation", False)),
                "available": self.wake.available()},
        }

    def speak(self, text: str) -> AudioChunk:
        """Synthesize a spoken reply (simulation audio when simulated)."""
        return self.synthesizer.synthesize(text)

    def process(self, speech: AudioChunk | str, *,
                agent: str = "VoiceInterface",
                task_id: str = "",
                task_factory: Callable[[VoiceIntent], dict[str, Any]]
                | None = None,
                approval_token_id: str = "",
                require_wake: bool = True) -> VoiceLoopResult:
        """One voice interaction: wake → transcribe → authorize → act → reply.

        ``speech`` is either bounded simulated audio (an
        :class:`AudioChunk`) or, for the typed-input path, plain text —
        which is recorded honestly as ``input=text`` and never claimed as
        recognition.
        """
        result = VoiceLoopResult(ok=False, simulation=self.simulation)
        # 1. Wake gate (audio only; typed text is an explicit user action).
        if isinstance(speech, str):
            result.stages.append(
                {"stage": "wake", "detail": "typed input bypasses wake"})
            transcription = Transcription(
                text=speech.strip(), confidence=1.0,
                engine="forge-typed-input", simulation=self.simulation)
        else:
            try:
                detection = self.wake.detect(speech)
            except WakeWordError as exc:
                return self._fail(result, "wake",
                                  {"kind": exc.kind, "message": exc.message})
            result.stages.append(
                {"stage": "wake", "detail": detection.to_dict()})
            if not detection.detected:
                if require_wake:
                    return self._fail(
                        result, "wake",
                        {"kind": "no_wake_word",
                         "message": "No wake word detected; no command "
                                    "was processed."})
                result.stages.append(
                    {"stage": "wake",
                     "detail": "wake bypass requested explicitly"})
            # 2. Recognition.
            try:
                transcription = self.transcriber.transcribe(speech)
            except TranscriptionError as exc:
                return self._fail(result, "transcribe",
                                  {"kind": exc.kind, "message": exc.message})
        result.transcription = transcription.to_dict()
        result.stages.append({"stage": "transcribe",
                              "detail": transcription.to_dict()})
        # 3. A33 permission system: parse, authorize, (approve), act.
        command = VoiceCommand(text=transcription.text, agent=agent,
                               task_id=task_id)
        handled = self.voice.handle(
            command, task_factory=task_factory,
            approval_token_id=approval_token_id)
        result.intent = {"name": handled.intent.name,
                         "slots": dict(handled.intent.slots),
                         "confidence": handled.intent.confidence}
        result.permission = handled.permission.to_dict() \
            if handled.permission else None
        result.stages.append({"stage": "authorize",
                              "detail": result.permission or {}})
        if not handled.ok:
            result.reply_text = handled.message
            self._reply(result, handled.message)
            result.ok = False
            return result
        result.action = handled.task
        result.stages.append({"stage": "act", "detail": handled.task or {}})
        action_text = (handled.task or {}).get("text") \
            if isinstance(handled.task, dict) \
            and (handled.task or {}).get("kind") == "reply" else ""
        reply_text = action_text or f"Done. {handled.message}"
        result.reply_text = reply_text
        self._reply(result, reply_text)
        result.ok = True
        return result

    # -- internals ---------------------------------------------------------

    def _fail(self, result: VoiceLoopResult, stage: str,
              error: dict[str, Any]) -> VoiceLoopResult:
        result.error = error
        result.stages.append({"stage": stage, "detail": {"error": error}})
        return result

    def _reply(self, result: VoiceLoopResult, text: str) -> bool:
        try:
            result.response_audio = self.synthesizer.synthesize(text)
            result.response_engine = self.synthesizer.name
            result.response_simulation = bool(getattr(
                self.synthesizer, "simulation", False))
            result.stages.append({"stage": "reply",
                                  "detail": {"text": text}})
            return True
        except SynthesisError as exc:
            result.stages.append({"stage": "reply", "detail": {
                "error": {"kind": exc.kind, "message": exc.message}}})
            return False
