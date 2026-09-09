"""Simulated speech codec (A36): deterministic audio<->text transport.

The codec encodes text as tones and decodes exactly what it encoded. It
is a data codec over PCM — never a claim of real speech recognition.
Arbitrary audio fails closed on framing and checksum.
"""
from __future__ import annotations

import pytest

from forge.voice.codec import (CodecError, decode_text, detect_wake,
                               encode_text, speech_chunk, wake_chunk,
                               wake_samples)


def test_encode_decode_round_trip():
    for text in ("run the tests", "commit the changes", "check status",
                 "summarize forge/voice/session.py", "z" * 150):
        samples = encode_text(text)
        assert decode_text(samples) == text


def test_encoding_is_deterministic():
    first = encode_text("review the diff")
    second = encode_text("review the diff")
    assert first == second
    assert len(first) == len(second)


def test_encoded_chunk_is_valid_wav_audio():
    chunk = speech_chunk("run the tests")
    wav = chunk.wav()
    assert wav[:4] == b"RIFF"
    assert chunk.duration_ms > 0


def test_empty_and_oversized_text_rejected():
    with pytest.raises(ValueError):
        encode_text("")
    with pytest.raises(ValueError):
        encode_text("   ")
    with pytest.raises(ValueError):
        encode_text("x" * 201)


def test_decode_rejects_silence_short_and_noise():
    with pytest.raises(CodecError) as info:
        decode_text(tuple(0 for _ in range(4000)))
    assert info.value.kind == "too_short"
    with pytest.raises(CodecError) as info:
        decode_text(tuple(0 for _ in range(16000)))
    assert info.value.kind == "no_preamble"
    with pytest.raises(CodecError):
        decode_text(encode_text("hello")[:1600])  # truncated
    import random
    random.seed(11)
    noise = tuple(random.randint(-3000, 3000) for _ in range(8000))
    with pytest.raises(CodecError):
        decode_text(noise)


def test_decode_rejects_tampered_checksum():
    # Build a well-formed message with a WRONG checksum byte by hand, so
    # the framing is perfect and only the checksum mismatches.
    from forge.voice import codec
    frames = list(codec._tone_frames(codec._TONE_ZERO, 3))
    payload = b"run the tests"
    frames += codec._byte_frames(len(payload))
    for byte in payload:
        frames += codec._byte_frames(byte)
    frames += codec._byte_frames((sum(payload) + 1) % 256)  # tampered
    frames += codec._tone_frames(codec._TONE_ZERO, 2)
    samples = tuple(s for frame in frames for s in frame)
    with pytest.raises(CodecError) as info:
        decode_text(samples)
    assert info.value.kind == "bad_checksum"


def test_wake_marker_round_trip():
    samples = wake_samples()
    assert detect_wake(samples)
    chunk = wake_chunk()
    assert detect_wake(chunk.samples())
    # Speech (non-wake) audio does not trigger the wake marker.
    assert not detect_wake(encode_text("run the tests"))
    assert not detect_wake(tuple(0 for _ in range(3200)))
