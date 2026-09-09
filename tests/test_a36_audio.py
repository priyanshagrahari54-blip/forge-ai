"""Audio primitives for the voice layer (A36).

Deterministic WAV encode/decode through the standard library, strict
format validation, bounded sizes/durations, and honest energy levels.
"""
from __future__ import annotations

import io
import struct
import wave

import pytest

from forge.voice.audio import (MAX_AUDIO_BYTES, AudioChunk, AudioError,
                               audio_level, chunk_from_samples, is_silence,
                               read_wav, validate_chunk)


def make_chunk(samples=8000) -> AudioChunk:
    data = struct.pack(f"<{samples}h", *([0] * samples))
    return AudioChunk(data=data)


def test_wav_round_trip_preserves_samples():
    samples = tuple((i * 37) % 20000 - 10000 for i in range(1600))
    chunk = chunk_from_samples(samples)
    wav = chunk.wav()
    assert wav[:4] == b"RIFF"
    parsed = read_wav(wav)
    assert parsed.sample_rate == chunk.sample_rate
    assert parsed.samples() == samples
    assert parsed.duration_ms == 100


def test_duration_ms_matches_frames():
    chunk = make_chunk(samples=16000)  # 1 second at 16 kHz
    assert chunk.duration_ms == 1000


def test_validation_rejects_bad_formats_and_bounds():
    with pytest.raises(AudioError) as info:
        validate_chunk(AudioChunk(data=b"", sample_rate=8000))
    assert info.value.kind == "unsupported"
    with pytest.raises(AudioError) as info:
        validate_chunk(AudioChunk(data=b"\x00"))  # not frame-aligned
    assert info.value.kind == "malformed"
    with pytest.raises(AudioError) as info:
        validate_chunk(make_chunk(samples=MAX_AUDIO_BYTES // 2 + 1))
    assert info.value.kind == "too_large"


def test_read_wav_rejects_garbage_and_oversize():
    with pytest.raises(AudioError) as info:
        read_wav(b"not a wav file at all")
    assert info.value.kind == "malformed"
    with pytest.raises(AudioError):
        read_wav(b"RIFF" + b"\x00" * (MAX_AUDIO_BYTES + 8192))


def test_read_wav_rejects_wrong_sampling():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(44100)
        out.writeframes(b"\x00\x00" * 800)
    with pytest.raises(AudioError) as info:
        read_wav(buffer.getvalue())
    assert info.value.kind == "unsupported"


def test_audio_level_and_silence_detection():
    silence = make_chunk(samples=1600)
    assert audio_level(silence) == 0.0
    assert is_silence(silence)
    loud = chunk_from_samples(tuple(20000 for _ in range(1600)))
    assert 0.5 < audio_level(loud) <= 1.0
    assert not is_silence(loud)


def test_chunk_from_samples_clamps_to_int16():
    chunk = chunk_from_samples((40000, -40000, 1))
    assert chunk.samples() == (32767, -32768, 1)
