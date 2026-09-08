"""Text-to-speech layer (A36).

Synthesis is a capability, not a claim: providers synthesize bounded text
into WAV-ready PCM or fail with a structured error. The simulated provider
emits the deterministic simulated-speech codec — clearly labeled
simulation audio, never synthetic human speech. Real synthesizers plug in
behind the same protocol.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from forge.voice.audio import AudioChunk
from forge.voice.codec import encode_text


class SynthesisError(Exception):
    """Speech could not be synthesized (structured, honest)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@runtime_checkable
class TextToSpeechProvider(Protocol):
    """Synthesize bounded text into mono 16 kHz PCM audio."""

    name: str
    simulation: bool

    def available(self) -> bool:
        ...

    def synthesize(self, text: str) -> AudioChunk:
        ...


class SimulatedSpeechSynthesizer:
    """Deterministic tone-codec "speech" (explicitly simulated)."""

    name = "forge-simulated-tts"
    simulation = True

    def available(self) -> bool:
        return True

    def synthesize(self, text: str) -> AudioChunk:
        try:
            samples = encode_text(text)
        except ValueError as exc:
            raise SynthesisError("invalid_text", str(exc)) from exc
        from forge.voice.codec import chunk_from_samples
        return chunk_from_samples(samples)


class UnconfiguredSpeechSynthesizer:
    """Honest stand-in when no real synthesizer is configured."""

    name = "unconfigured-tts"
    simulation = False

    def available(self) -> bool:
        return False

    def synthesize(self, text: str) -> AudioChunk:
        del text
        raise SynthesisError(
            "not_configured",
            "No speech synthesizer is configured. Register a "
            "TextToSpeechProvider or use the simulated provider.")
