"""Wake-word layer (A36).

The wake gate exists so ambient audio never reaches the command pipeline:
recognition and execution only run after a wake detection. The simulated
detector recognizes exactly the deterministic wake marker emitted by the
simulated transport — real audio is not detected, and no real wake-word
model is claimed. Real detectors plug in behind the same protocol.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from forge.voice.audio import AudioChunk, validate_chunk
from forge.voice.codec import detect_wake


class WakeWordError(Exception):
    """Wake detection could not run (structured, honest)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class WakeDetection:
    detected: bool
    word: str = ""
    confidence: float = 0.0
    engine: str = ""
    simulation: bool = False

    def to_dict(self) -> dict:
        return {
            "detected": self.detected,
            "word": self.word,
            "confidence": self.confidence,
            "engine": self.engine,
            "simulation": self.simulation,
        }


@runtime_checkable
class WakeWordDetector(Protocol):
    """Detect the wake word in bounded mono 16 kHz PCM audio."""

    name: str
    simulation: bool

    def available(self) -> bool: ...

    def detect(self, chunk: AudioChunk) -> WakeDetection: ...


class SimulatedWakeWordDetector:
    """Detects exactly the simulated wake marker (simulation only)."""

    name = "forge-simulated-wake"
    simulation = True
    word = "forge"

    def __init__(self) -> None:
        self._injected = False

    def available(self) -> bool:
        return True

    def inject(self) -> None:
        """Simulate a hardware wake trigger (explicit, tests/dev only)."""
        self._injected = True

    def detect(self, chunk: AudioChunk) -> WakeDetection:
        validate_chunk(chunk)
        detected = self._injected or detect_wake(chunk.samples())
        self._injected = False
        if detected:
            return WakeDetection(True, word=self.word, confidence=1.0,
                                 engine=self.name, simulation=True)
        return WakeDetection(False, word=self.word, confidence=0.0,
                             engine=self.name, simulation=True)


class UnconfiguredWakeWordDetector:
    """Honest stand-in when no real wake model is configured."""

    name = "unconfigured-wake"
    simulation = False

    def available(self) -> bool:
        return False

    def detect(self, chunk: AudioChunk) -> WakeDetection:
        del chunk
        raise WakeWordError(
            "not_configured",
            "No wake-word detector is configured. Register a "
            "WakeWordDetector or use the simulated provider.")
