"""Audio primitives for the voice layer (A36).

Deterministic, dependency-free PCM/WAV handling: bounded chunks, strict
format validation, honest energy levels, and WAV encode/decode through the
standard library. This module performs real signal plumbing — no
recognition or synthesis claims live here.
"""
from __future__ import annotations

import io
import math
import struct
import wave
from dataclasses import dataclass

#: Voice audio bounds. 16 kHz mono 16-bit PCM keeps everything small and
#: deterministic; 500 KB is ~15.6 s of audio.
SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2  # bytes per sample (16-bit signed little-endian PCM)
MAX_AUDIO_BYTES = 500_000
MAX_AUDIO_MS = 60_000
FRAME_SAMPLES = 320  # 20 ms at 16 kHz


class AudioError(Exception):
    """Audio data is malformed, unsupported, or out of bounds."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class AudioChunk:
    """Raw 16-bit mono 16 kHz PCM audio."""

    data: bytes
    sample_rate: int = SAMPLE_RATE
    channels: int = CHANNELS
    sample_width: int = SAMPLE_WIDTH

    @property
    def duration_ms(self) -> int:
        bytes_per_sample = self.sample_width * self.channels
        frames = len(self.data) // bytes_per_sample
        return int(round(frames / self.sample_rate * 1000))

    def samples(self) -> tuple[int, ...]:
        """Signed 16-bit samples (mono)."""
        count = len(self.data) // self.sample_width
        return struct.unpack(f"<{count}h", self.data[:count * self.sample_width])

    def wav(self) -> bytes:
        """Serialize as a standard RIFF/WAVE file."""
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as out:
            out.setnchannels(self.channels)
            out.setsampwidth(self.sample_width)
            out.setframerate(self.sample_rate)
            out.writeframes(self.data)
        return buffer.getvalue()


def chunk_from_samples(samples: tuple[int, ...]) -> AudioChunk:
    data = struct.pack(f"<{len(samples)}h",
                       *(max(-32768, min(32767, int(s))) for s in samples))
    return AudioChunk(data=data)


def validate_chunk(chunk: AudioChunk) -> None:
    """Enforce the supported format and bounds (fail closed)."""
    if not isinstance(chunk.data, bytes):
        raise AudioError("invalid", "audio data must be bytes")
    if chunk.sample_rate != SAMPLE_RATE or chunk.channels != CHANNELS \
            or chunk.sample_width != SAMPLE_WIDTH:
        raise AudioError(
            "unsupported",
            f"audio must be {SAMPLE_RATE} Hz mono 16-bit PCM; got "
            f"{chunk.sample_rate} Hz, {chunk.channels} ch, "
            f"{chunk.sample_width * 8}-bit")
    if len(chunk.data) % (SAMPLE_WIDTH * CHANNELS) != 0:
        raise AudioError("malformed", "audio data is not frame-aligned")
    if len(chunk.data) > MAX_AUDIO_BYTES:
        raise AudioError("too_large", "audio exceeds the size bound")
    if chunk.duration_ms > MAX_AUDIO_MS:
        raise AudioError("too_long", "audio exceeds the duration bound")


def read_wav(data: bytes) -> AudioChunk:
    """Parse bounded WAV input into an :class:`AudioChunk` (strict)."""
    if not data or len(data) > MAX_AUDIO_BYTES + 4096:
        raise AudioError("too_large", "audio exceeds the size bound")
    try:
        with wave.open(io.BytesIO(data), "rb") as source:
            rate = source.getframerate()
            channels = source.getnchannels()
            width = source.getsampwidth()
            frames = source.readframes(MAX_AUDIO_BYTES // max(1, width))
    except (wave.Error, EOFError, struct.error) as exc:
        raise AudioError("malformed", "not a valid WAV file") from exc
    if len(frames) > MAX_AUDIO_BYTES:
        raise AudioError("too_large", "audio exceeds the size bound")
    chunk = AudioChunk(data=frames, sample_rate=rate, channels=channels,
                       sample_width=width)
    validate_chunk(chunk)
    return chunk


def audio_level(chunk: AudioChunk) -> float:
    """RMS energy in [0, 1] — deterministic, threshold-free."""
    samples = chunk.samples()
    if not samples:
        return 0.0
    total = sum(s * s for s in samples) / len(samples)
    return math.sqrt(total) / 32768.0


def is_silence(chunk: AudioChunk, threshold: float = 0.02) -> bool:
    """Whether a chunk carries (essentially) no energy."""
    return audio_level(chunk) <= threshold
