"""Wake-word layer (A36): the gate before any command processing."""
from __future__ import annotations

import pytest

from forge.voice.audio import AudioChunk
from forge.voice.codec import speech_chunk, wake_chunk
from forge.voice.wake import (SimulatedWakeWordDetector,
                              UnconfiguredWakeWordDetector, WakeDetection,
                              WakeWordError)


def test_simulated_detects_marker_only():
    wake = SimulatedWakeWordDetector()
    assert wake.available() and wake.simulation
    detection = wake.detect(wake_chunk())
    assert isinstance(detection, WakeDetection)
    assert detection.detected and detection.word == "forge"
    assert detection.simulation
    # Non-marker audio: no detection (never a fabricated wake).
    assert not wake.detect(speech_chunk("run the tests")).detected
    assert not wake.detect(AudioChunk(data=b"\x00\x00" * 800)).detected


def test_inject_simulates_hardware_trigger_once():
    wake = SimulatedWakeWordDetector()
    wake.inject()
    assert wake.detect(speech_chunk("run the tests")).detected
    # Injection is single-shot.
    assert not wake.detect(speech_chunk("run the tests")).detected


def test_unconfigured_fails_with_not_configured():
    wake = UnconfiguredWakeWordDetector()
    assert not wake.available()
    with pytest.raises(WakeWordError) as info:
        wake.detect(wake_chunk())
    assert info.value.kind == "not_configured"
