"""Deterministic simulated speech codec (A36).

The simulated voice transport encodes text as a deterministic sequence of
audio tones. The companion simulated speech-to-text decodes exactly this
encoding — a byte-for-byte round trip, never a claim of recognizing real
speech. Arbitrary audio (e.g. a microphone recording) does not match the
frame structure and checksum, so the simulated recognizer refuses it
instead of hallucinating a transcription.

This is a data codec over PCM, honest and fully deterministic: the same
text always produces the same audio, and decoding audio that was not
produced by the encoder fails closed.
"""
from __future__ import annotations

import math

from forge.voice.audio import (FRAME_SAMPLES, SAMPLE_RATE, AudioChunk,
                                chunk_from_samples)

#: Tone frequencies (Hz) for the codec alphabet.
_TONE_ONE = 1500.0   # data bit 1
_TONE_ZERO = 1000.0  # data bit 0 / framing
_TONE_WAKE = 700.0   # wake marker

#: Zero-crossings per 20 ms frame at 16 kHz for each tone.
_XING = {
    _TONE_ONE: int(2 * _TONE_ONE * (FRAME_SAMPLES / SAMPLE_RATE)),
    _TONE_ZERO: int(2 * _TONE_ZERO * (FRAME_SAMPLES / SAMPLE_RATE)),
    _TONE_WAKE: int(2 * _TONE_WAKE * (FRAME_SAMPLES / SAMPLE_RATE)),
}

MAX_TEXT_CHARS = 200
MAX_TEXT_BYTES = 250
_AMPLITUDE = 0.55


def _tone_frames(frequency: float, count: int) -> list[tuple[int, ...]]:
    frames = []
    for frame in range(count):
        samples = []
        for index in range(FRAME_SAMPLES):
            time = (frame * FRAME_SAMPLES + index) / SAMPLE_RATE
            value = _AMPLITUDE * 32767.0 * math.sin(2 * math.pi * frequency
                                                    * time)
            samples.append(int(value))
        frames.append(tuple(samples))
    return frames


def _silence_frames(count: int) -> list[tuple[int, ...]]:
    return [tuple(0 for _ in range(FRAME_SAMPLES)) for _ in range(count)]


def _byte_frames(byte: int) -> list[tuple[int, ...]]:
    frames = []
    for shift in range(7, -1, -1):
        frequency = _TONE_ONE if (byte >> shift) & 1 else _TONE_ZERO
        frames.extend(_tone_frames(frequency, 1))
    return frames


def encode_text(text: str) -> tuple[int, ...]:
    """Encode text as deterministic PCM samples (speech simulation)."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"text is limited to {MAX_TEXT_CHARS} characters")
    payload = text.encode("utf-8")
    if len(payload) > MAX_TEXT_BYTES:
        raise ValueError(f"text encodes to more than {MAX_TEXT_BYTES} bytes")
    checksum = sum(payload) % 256
    message = bytes([len(payload)]) + payload + bytes([checksum])
    samples: list[int] = []
    for frame in _tone_frames(_TONE_ZERO, 3):  # preamble: framing
        samples.extend(frame)
    for byte in message:
        for frame in _byte_frames(byte):
            samples.extend(frame)
    for frame in _tone_frames(_TONE_ZERO, 2):  # trailer
        samples.extend(frame)
    samples.extend(0 for _ in range(FRAME_SAMPLES))  # closing silence
    return tuple(samples)


def decode_text(samples: tuple[int, ...]) -> str:
    """Decode samples produced by :func:`encode_text` (fail closed).

    Raises :class:`CodecError` for anything that is not a well-formed
    simulated-speech frame stream (including arbitrary real audio).
    """
    expected_frames = len(samples) // FRAME_SAMPLES
    if expected_frames < 16:
        raise CodecError("too_short", "audio is too short to carry a message")
    bits: list[str] = []
    for frame_index in range(expected_frames):
        start = frame_index * FRAME_SAMPLES
        crossings = _zero_crossings(samples[start:start + FRAME_SAMPLES])
        if crossings >= 55:
            bits.append("1")
        elif crossings >= 30:
            bits.append("0")
        else:
            bits.append("_")
    cursor = 0
    while cursor < len(bits) and bits[cursor] == "_":
        cursor += 1
    if cursor + 3 > len(bits) or bits[cursor:cursor + 3] != ["0"] * 3:
        raise CodecError("no_preamble",
                         "audio does not start with the simulated-speech "
                         "preamble")
    cursor += 3

    def read_byte(position: int) -> int:
        if position + 8 > len(bits):
            raise CodecError("truncated", "audio ended mid-message")
        value = 0
        for bit in bits[position:position + 8]:
            if bit == "_":
                raise CodecError("malformed",
                                 "audio has gaps inside a message byte")
            value = (value << 1) | (1 if bit == "1" else 0)
        return value

    length = read_byte(cursor)
    cursor += 8
    payload = bytearray()
    for _ in range(length):
        payload.append(read_byte(cursor))
        cursor += 8
    checksum = read_byte(cursor)
    cursor += 8
    if cursor + 2 > len(bits) or bits[cursor:cursor + 2] != ["0"] * 2:
        raise CodecError("no_trailer", "audio does not end with the "
                         "simulated-speech trailer")
    if sum(payload) % 256 != checksum:
        raise CodecError("bad_checksum",
                         "audio does not match the simulated-speech "
                         "checksum")
    try:
        return bytes(payload).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CodecError("bad_text", "decoded payload is not UTF-8") from exc


def wake_samples() -> tuple[int, ...]:
    """Deterministic wake-marker audio (simulation only)."""
    samples: list[int] = []
    for frame in _tone_frames(_TONE_WAKE, 5):
        samples.extend(frame)
    samples.extend(0 for _ in range(FRAME_SAMPLES * 2))
    return tuple(samples)


def detect_wake(samples: tuple[int, ...]) -> bool:
    """Detect the wake marker produced by :func:`wake_samples`."""
    frames = len(samples) // FRAME_SAMPLES
    runs = 0
    for frame_index in range(frames):
        start = frame_index * FRAME_SAMPLES
        crossings = _zero_crossings(samples[start:start + FRAME_SAMPLES])
        wake_x = _XING[_TONE_WAKE]
        if abs(crossings - wake_x) <= 4:
            runs += 1
            if runs >= 3:
                return True
        else:
            runs = 0
    return False


def speech_chunk(text: str) -> AudioChunk:
    """A WAV-ready simulated utterance for *text*."""
    return chunk_from_samples(encode_text(text))


def wake_chunk() -> AudioChunk:
    """A WAV-ready simulated wake word utterance."""
    return chunk_from_samples(wake_samples())


class CodecError(Exception):
    """The audio is not decodable as simulated speech."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def _zero_crossings(frame: tuple[int, ...]) -> int:
    crossings = 0
    previous = 0
    for value in frame:
        if (previous < 0 <= value) or (previous >= 0 > value):
            crossings += 1
        previous = value
    return crossings
