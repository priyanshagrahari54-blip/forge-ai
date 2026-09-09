"""Real OpenAI speech-to-text provider (Whisper) for A36.

Requires ``OPENAI_API_KEY``. Uses the OpenAI Audio Transcriptions API
(Whisper). The provider is labeled ``simulation=False`` and carries the
real engine name. Audio must be bounded mono 16 kHz PCM, converted to
WAV before sending.

All existing security invariants hold: voice remains a
permission-gated request source, and transcription output is treated
as untrusted input.
"""
from __future__ import annotations

import io
import os
import struct
import time
from typing import Any

from forge.voice.audio import AudioChunk, validate_chunk
from forge.voice.transcriber import (
    SpeechToTextProvider,
    Transcription,
    TranscriptionError,
)

DEFAULT_MODEL = "whisper-1"
REQUEST_TIMEOUT = 30.0


class OpenAISpeechToText:
    """Real OpenAI Whisper speech-to-text.

    Requires ``OPENAI_API_KEY``; returns ``available()=False`` when unset.
    """

    name = "openai-whisper"
    simulation = False

    def __init__(self, *, model: str = "", api_key: str = "") -> None:
        self.model = model or os.environ.get(
            "FORGE_WHISPER_MODEL", DEFAULT_MODEL)
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")

    def available(self) -> bool:
        return bool(self._api_key)

    def transcribe(self, chunk: AudioChunk) -> Transcription:
        validate_chunk(chunk)
        if not self._api_key:
            raise TranscriptionError(
                "not_configured",
                "No OPENAI_API_KEY configured for Whisper.")
        try:
            text = self._call_api(chunk)
        except Exception as exc:
            raise TranscriptionError(
                "api_error",
                f"OpenAI Whisper API error: {exc}") from exc
        if not text.strip():
            raise TranscriptionError(
                "unrecognized",
                "Whisper returned empty transcription.")
        return Transcription(
            text=text.strip(), confidence=0.9,
            engine=self.name, simulation=False)

    def _call_api(self, chunk: AudioChunk) -> str:
        import json
        import urllib.request

        wav_data = chunk.wav()
        boundary = "----ForgeBoundary"
        body_parts: list[bytes] = []
        # file field
        body_parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; '
            f'filename="audio.wav"\r\n'
            f"Content-Type: audio/wav\r\n\r\n".encode("utf-8"))
        body_parts.append(wav_data)
        body_parts.append(b"\r\n")
        # model field
        body_parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="model"\r\n\r\n'
            f"{self.model}\r\n".encode("utf-8"))
        # response_format
        body_parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="response_format"'
            f"\r\n\r\njson\r\n".encode("utf-8"))
        body_parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(body_parts)

        url = "https://api.openai.com/v1/audio/transcriptions"
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type":
                    f"multipart/form-data; boundary={boundary}",
            })
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("text", "")
