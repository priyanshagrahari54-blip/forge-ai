"""Speech-to-text layer (A36): honest recognition, structured failures.

The simulated recognizer decodes only simulated-codec audio; real (arbitrary)
audio is refused with a structured error instead of a fabricated
transcription.
"""
from __future__ import annotations

import random
import struct

import pytest

from forge.voice.audio import AudioChunk
from forge.voice.codec import speech_chunk
from forge.voice.transcriber import (SimulatedSpeechToText, Transcription,
                                     TranscriptionError,
                                     UnconfiguredSpeechToText)


def white_noise_chunk(samples=8000) -> AudioChunk:
    random.seed(42)
    data = struct.pack(f"<{samples}h",
                       *[random.randint(-3000, 3000)
                         for _ in range(samples)])
    return AudioChunk(data=data)


def test_simulated_round_trip_and_labeling():
    stt = SimulatedSpeechToText()
    transcription = stt.transcribe(speech_chunk("run the tests"))
    assert isinstance(transcription, Transcription)
    assert transcription.text == "run the tests"
    assert transcription.confidence == 1.0
    assert transcription.simulation is True
    assert "simulated" in transcription.engine
    assert stt.available()


def test_simulated_refuses_arbitrary_audio():
    stt = SimulatedSpeechToText()
    with pytest.raises(TranscriptionError) as info:
        stt.transcribe(white_noise_chunk())
    assert info.value.kind == "unrecognized"
    # The refusal says what it is honestly: no real recognition claimed.
    assert "simulated" in info.value.message


def test_simulated_refuses_silence_and_wrong_format():
    stt = SimulatedSpeechToText()
    with pytest.raises(TranscriptionError):
        stt.transcribe(AudioChunk(data=struct.pack("<8000h", *([0] * 8000))))
    with pytest.raises(Exception):
        stt.transcribe(AudioChunk(data=b"", sample_rate=8000))


def test_unconfigured_fails_with_not_configured():
    stt = UnconfiguredSpeechToText()
    assert not stt.available()
    with pytest.raises(TranscriptionError) as info:
        stt.transcribe(speech_chunk("anything"))
    assert info.value.kind == "not_configured"
