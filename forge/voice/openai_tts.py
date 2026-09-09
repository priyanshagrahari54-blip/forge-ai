"""Real OpenAI text-to-speech provider for A36.

Requires ``OPENAI_API_KEY``. Uses the OpenAI Audio Speech API (TTS).
The provider is labeled ``simulation=False`` and carries the real engine
name. Output is converted to mono 16 kHz PCM for the A36 audio pipeline.

All existing security invariants hold.
"""
from __future__ import annotations

import io
import os
import struct
from typing import Any

from forge.voice.audio import AudioChunk
from forge.voice.synthesizer import (
    TextToSpeechProvider,
    SynthesisError,
)

DEFAULT_MODEL = "tts-1"
REQUEST_TIMEOUT = 30.0


class OpenAISpeechSynthesizer:
    """Real OpenAI text-to-speech.

    Requires ``OPENAI_API_KEY``; returns ``available()=False`` when unset.
    """

    name = "openai-tts"
    simulation = False

    def __init__(self, *, model: str = "", api_key: str = "",
                 voice: str = "alloy") -> None:
        self.model = model or os.environ.get(
            "FORGE_TTS_MODEL", DEFAULT_MODEL)
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._voice = os.environ.get("FORGE_TTS_VOICE", voice)

    def available(self) -> bool:
        return bool(self._api_key)

    def synthesize(self, text: str) -> AudioChunk:
        if not self._api_key:
            raise SynthesisError(
                "not_configured",
                "No OPENAI_API_KEY configured for TTS.")
        try:
            return self._call_api(text)
        except SynthesisError:
            raise
        except Exception as exc:
            raise SynthesisError(
                "api_error",
                f"OpenAI TTS API error: {exc}") from exc

    def _call_api(self, text: str) -> AudioChunk:
        import json
        import urllib.request

        body = json.dumps({
            "model": self.model,
            "input": text[:4000],
            "voice": self._voice,
            "response_format": "pcm",
        }).encode("utf-8")

        url = "https://api.openai.com/v1/audio/speech"
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            })
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            pcm_data = resp.read()

        # OpenAI TTS returns 24 kHz 16-bit mono PCM.
        # Convert to 16 kHz for the A36 pipeline.
        src_rate = 24000
        dst_rate = 16000
        samples = []
        for i in range(0, len(pcm_data) - 1, 2):
            samples.append(struct.unpack_from("<h", pcm_data, i)[0])
        # Simple linear interpolation downsample
        if src_rate != dst_rate and samples:
            ratio = src_rate / dst_rate
            resampled = []
            pos = 0.0
            while int(pos) < len(samples) - 1:
                idx = int(pos)
                frac = pos - idx
                val = samples[idx] * (1 - frac) + samples[idx + 1] * frac
                resampled.append(int(max(-32768, min(32767, val))))
                pos += ratio
            samples = resampled

        from forge.voice.codec import chunk_from_samples
        return chunk_from_samples(samples)
