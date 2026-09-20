"""Self-hosted speech providers: real STT and TTS on your own server.

The OpenAI-backed providers need ``OPENAI_API_KEY``; these use an
OpenAI-compatible endpoint you run yourself — whisper.cpp's server,
faster-whisper-server, or vLLM for transcription, and Kokoro / Piper /
openedai-speech style servers for synthesis:

```bash
# transcription (any OpenAI-compatible /v1/audio/transcriptions)
FORGE_STT_URL=http://gpu-box:8081
FORGE_STT_MODEL=whisper-large-v3-turbo
FORGE_STT_LANGUAGE=auto          # or hi, en — whisper detects Hinglish as hi/en

# synthesis (any OpenAI-compatible /v1/audio/speech)
FORGE_TTS_URL=http://gpu-box:8082
FORGE_TTS_MODEL=kokoro
FORGE_TTS_VOICE=hindi_female
FORGE_TTS_FORMAT=pcm             # pcm (16-bit mono 16 kHz) or wav
```

Both providers follow the A36 protocols exactly, are labeled
``simulation=False``, and carry the real engine name. Missing configuration is
reported as ``not_configured``, never as a silent success. Audio in is bounded
mono 16 kHz PCM and is sent as WAV; audio out is converted to the same shape.
"""
from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.request
import wave

from forge.voice.audio import AudioChunk, validate_chunk
from forge.voice.synthesizer import SynthesisError
from forge.voice.transcriber import Transcription, TranscriptionError

#: Fixed endpoints for the OpenAI-compatible audio API shape.
TRANSCRIPTIONS_PATH = "/v1/audio/transcriptions"
SPEECH_PATH = "/v1/audio/speech"
REQUEST_TIMEOUT = 120.0
MAX_INPUT_CHARS = 4000


def normalize_base(url: str) -> str:
    """Accept ``host``, ``host/`` or ``host/v1`` and return ``host`` (no /v1)."""
    base = str(url).strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


class LocalSpeechToText:
    """Speech-to-text against a self-hosted OpenAI-compatible endpoint."""

    name = "local-whisper"
    simulation = False

    def __init__(self, *, model: str = "", url: str = "",
                 language: str = "", api_key: str = "",
                 timeout: float = REQUEST_TIMEOUT) -> None:
        self.base_url = normalize_base(
            url or os.environ.get("FORGE_STT_URL", ""))
        self.model = model or os.environ.get("FORGE_STT_MODEL", "whisper-1")
        #: Empty/"auto" lets the recognizer detect the language, which is what
        #: makes mixed Hindi/English speech work without a hint.
        raw_language = (language or os.environ.get("FORGE_STT_LANGUAGE", "")
                        or "").strip()
        self.language = "" if raw_language.lower() in ("", "auto") \
            else raw_language
        self.api_key = api_key or os.environ.get("FORGE_STT_KEY", "")
        self.timeout = float(timeout)

    def available(self) -> bool:
        return bool(self.base_url)

    # -- protocol ------------------------------------------------------------

    def transcribe(self, chunk: AudioChunk) -> Transcription:
        validate_chunk(chunk)
        if not self.base_url:
            raise TranscriptionError(
                "not_configured",
                "No self-hosted speech recognizer is configured. Set "
                "FORGE_STT_URL to an OpenAI-compatible /v1/audio/transcriptions "
                "endpoint (e.g. whisper.cpp's server).")
        try:
            payload = self._post(chunk)
        except urllib.error.HTTPError as exc:
            detail = _http_detail(exc)
            raise TranscriptionError(
                "api_error",
                f"{self.base_url}{TRANSCRIPTIONS_PATH} returned HTTP "
                f"{exc.code}: {detail}") from exc
        except Exception as exc:                              # noqa: BLE001
            raise TranscriptionError(
                "api_error",
                f"self-hosted transcription failed at {self.base_url}: {exc}"
            ) from exc

        text = str(payload.get("text") or "").strip()
        if not text:
            raise TranscriptionError(
                "unrecognized",
                "The recognizer returned an empty transcription.")
        language = str(payload.get("language") or self.language or "en")
        return Transcription(
            text=text,
            #: Whisper-style APIs report word/segment detail, not a confidence
            #: scalar; 1.0 here would be a fabricated certainty.
            confidence=_confidence_from_payload(payload),
            engine=self.name, simulation=False, language=language)

    def _post(self, chunk: AudioChunk) -> dict:
        boundary = "----ForgeLocalAudioBoundary"
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="{name}"\r\n\r\n{value}\r\n'.encode())

        field("model", self.model)
        if self.language:
            field("language", self.language)
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; "
            f'name="file"; filename="audio.wav"\r\n'
            f"Content-Type: audio/wav\r\n\r\n".encode())
        parts.append(chunk.wav())
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)

        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.base_url + TRANSCRIPTIONS_PATH, data=body, method="POST",
            headers=headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


class LocalSpeechSynthesizer:
    """Text-to-speech against a self-hosted OpenAI-compatible endpoint."""

    name = "local-tts"
    simulation = False

    def __init__(self, *, model: str = "", url: str = "", voice: str = "",
                 response_format: str = "", api_key: str = "",
                 timeout: float = REQUEST_TIMEOUT) -> None:
        self.base_url = normalize_base(
            url or os.environ.get("FORGE_TTS_URL", ""))
        self.model = model or os.environ.get("FORGE_TTS_MODEL", "tts-1")
        self.voice = voice or os.environ.get("FORGE_TTS_VOICE", "alloy")
        #: "pcm" is 16-bit mono 16 kHz, ready for the A36 pipeline; "wav" is
        #: resampled to the same shape when the server offers only files.
        self.response_format = (
            response_format or os.environ.get("FORGE_TTS_FORMAT", "pcm")
        ).strip().lower()
        self.api_key = api_key or os.environ.get("FORGE_TTS_KEY", "")
        self.timeout = float(timeout)

    def available(self) -> bool:
        return bool(self.base_url)

    # -- protocol ------------------------------------------------------------

    def synthesize(self, text: str) -> AudioChunk:
        if not self.base_url:
            raise SynthesisError(
                "not_configured",
                "No self-hosted speech synthesizer is configured. Set "
                "FORGE_TTS_URL to an OpenAI-compatible /v1/audio/speech "
                "endpoint (e.g. Kokoro or Piper).")
        spoken = str(text or "").strip()
        if not spoken:
            raise SynthesisError("empty_input", "Nothing to synthesize.")
        try:
            audio = self._post(spoken[:MAX_INPUT_CHARS])
        except urllib.error.HTTPError as exc:
            detail = _http_detail(exc)
            raise SynthesisError(
                "api_error",
                f"{self.base_url}{SPEECH_PATH} returned HTTP {exc.code}: "
                f"{detail}") from exc
        except SynthesisError:
            raise
        except Exception as exc:                              # noqa: BLE001
            raise SynthesisError(
                "api_error",
                f"self-hosted synthesis failed at {self.base_url}: {exc}"
            ) from exc
        if not audio:
            raise SynthesisError(
                "api_error", "The synthesizer returned no audio.")
        return self._to_chunk(audio)

    def _post(self, text: str) -> bytes:
        body = json.dumps({
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": self.response_format,
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.base_url + SPEECH_PATH, data=body, method="POST",
            headers=headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read()

    def _to_chunk(self, audio: bytes) -> AudioChunk:
        """Accept raw 16-bit PCM or a WAV file; anything else is reported."""
        if audio[:4] == b"RIFF":
            try:
                with wave.open(io.BytesIO(audio), "rb") as handle:
                    channels, width, rate = (handle.getnchannels(),
                                             handle.getsampwidth(),
                                             handle.getframerate())
                    frames = handle.readframes(handle.getnframes())
            except Exception as exc:                          # noqa: BLE001
                raise SynthesisError(
                    "api_error", f"returned audio is not a valid WAV: {exc}"
                ) from exc
            if width != 2:
                raise SynthesisError(
                    "api_error",
                    f"returned WAV is {width * 8}-bit; 16-bit PCM is required")
            return _pcm_to_chunk(frames, sample_rate=rate, channels=channels)
        if self.response_format == "pcm":
            return AudioChunk(audio)
        raise SynthesisError(
            "api_error",
            f"the server returned {len(audio)} bytes that are neither WAV nor "
            f"the requested format {self.response_format!r}")


# -- helpers -------------------------------------------------------------------

def _pcm_to_chunk(data: bytes, *, sample_rate: int, channels: int) -> AudioChunk:
    """Convert interleaved PCM to the mono 16 kHz shape the pipeline requires."""
    from forge.voice.audio import CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH
    import struct

    if channels == CHANNELS and sample_rate == SAMPLE_RATE:
        return AudioChunk(data)
    if SAMPLE_WIDTH != 2:
        return AudioChunk(data)
    samples = struct.unpack(f"<{len(data) // 2}h", data[:len(data) // 2 * 2])
    if channels > 1:
        mono = [samples[index] for index in range(0, len(samples), channels)]
    else:
        mono = list(samples)
    if sample_rate != SAMPLE_RATE and mono:
        ratio = SAMPLE_RATE / float(sample_rate)
        target = max(1, int(len(mono) * ratio))
        resampled = []
        for position in range(target):
            source = min(len(mono) - 1, int(position / ratio))
            resampled.append(mono[source])
        mono = resampled
    return AudioChunk(struct.pack(f"<{len(mono)}h", *mono))


def _confidence_from_payload(payload: dict) -> float:
    """Use the provider's own numbers when it gives them; never invent one."""
    average = payload.get("avg_logprob", payload.get("avg_logprob_avg"))
    if isinstance(average, (int, float)):
        import math

        return max(0.0, min(1.0, math.exp(float(average))))
    for key in ("confidence", "probability"):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return max(0.0, min(1.0, float(value)))
    no_speech = payload.get("no_speech_prob")
    if isinstance(no_speech, (int, float)):
        return max(0.0, min(1.0, 1.0 - float(no_speech)))
    #: Unknown, not certain: the transcript is real, the confidence is not.
    return 0.5


def _http_detail(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")[:300]
    except Exception:                                         # noqa: BLE001
        return ""
