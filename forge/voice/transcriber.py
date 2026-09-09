"""Speech-to-text layer (A36).

Transcription is a capability, not a claim: providers transcribe bounded
audio or fail with a structured error. The simulated provider decodes only
the deterministic simulated-speech codec — arbitrary (real) audio is
refused, never "recognized" by a fabricated engine. Real recognizers plug
in behind the same protocol.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from forge.voice.audio import AudioChunk, validate_chunk
from forge.voice.codec import CodecError, decode_text


class TranscriptionError(Exception):
    """Transcription could not be produced (structured, honest)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class Transcription:
    text: str
    confidence: float
    engine: str
    simulation: bool
    language: str = "en"

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "engine": self.engine,
            "simulation": self.simulation,
            "language": self.language,
        }


@runtime_checkable
class SpeechToTextProvider(Protocol):
    """Transcribe bounded mono 16 kHz PCM into text."""

    name: str
    simulation: bool

    def available(self) -> bool: ...

    def transcribe(self, chunk: AudioChunk) -> Transcription: ...


class SimulatedSpeechToText:
    """Deterministic decoder for the simulated-speech codec.

    Only audio produced by :func:`forge.voice.codec.encode_text` decodes;
    anything else raises :class:`TranscriptionError` with kind
    ``unrecognized`` — the simulated engine never pretends to understand
    real speech.
    """

    name = "forge-simulated-stt"
    simulation = True

    def available(self) -> bool:
        return True

    def transcribe(self, chunk: AudioChunk) -> Transcription:
        validate_chunk(chunk)
        try:
            text = decode_text(chunk.samples())
        except CodecError as exc:
            raise TranscriptionError(
                "unrecognized",
                "Audio was not produced by the simulated speech codec; "
                "the simulated recognizer cannot transcribe real speech. "
                f"({exc.kind})") from exc
        if not text.strip():
            raise TranscriptionError(
                "unrecognized",
                "Audio carried no utterance (empty simulated message); "
                "nothing to transcribe.")
        return Transcription(text=text, confidence=1.0,
                             engine=self.name, simulation=True)


class UnconfiguredSpeechToText:
    """Honest stand-in when no real recognizer is configured."""

    name = "unconfigured-stt"
    simulation = False

    def available(self) -> bool:
        return False

    def transcribe(self, chunk: AudioChunk) -> Transcription:
        del chunk
        raise TranscriptionError(
            "not_configured",
            "No speech recognizer is configured. Register a "
            "SpeechToTextProvider or use the simulated provider.")
