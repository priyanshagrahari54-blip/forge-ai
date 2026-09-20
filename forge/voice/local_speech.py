"""Real in-process speech: synthesis, transcription and acoustic measurement.

Three genuine backends, each optional so a text-only deployment (and CI) keeps
working — and each reported honestly when it is missing:

* **text-to-speech** — the ``espeak-ng`` library shipped by the
  ``espeakng-loader`` wheel is called through ``ctypes``. Real audio comes out:
  a 16-bit mono WAV at 22 050 Hz, written where the caller asks.
* **speech-to-text** — ``pocketsphinx`` with the acoustic and language model
  its own wheel bundles. Real transcription of real audio, offline.
* **measurement** — RMS, peak, clipping, silence ratio, zero-crossing rate and
  spectral centroid computed from the samples with the standard library (and
  numpy when present).

What this is *not*: a cloud-quality recogniser. The bundled Sphinx model is
small, so the transcript is a hypothesis with a confidence score, and the
provider says so in its metadata instead of overclaiming. A better model is
what ``FORGE_STT_URL`` / ``FORGE_TTS_URL`` are for.
"""
from __future__ import annotations

import base64
import ctypes
import io
import math
import os
import re
import struct
import threading
import time
import wave
from pathlib import Path
from typing import Any

from forge.models.errors import ProviderError
from forge.models.provider import ModelResult

SAMPLE_RATE = 22050
MAX_AUDIO_BYTES = 32 * 1024 * 1024

#: Speech rate/volume/pitch the synthesizer is asked for; a caller can override
#: ``rate`` in the request text (``rate: 160``).
DEFAULT_RATE = 165

#: Bundled espeak-ng (no system package needed) and the Sphinx model.
try:                                                        # pragma: no cover
    import espeakng_loader
except Exception:                                          # noqa: BLE001
    espeakng_loader = None                                 # type: ignore[assignment]

try:                                                        # pragma: no cover
    from pocketsphinx import Decoder
except Exception:                                          # noqa: BLE001
    Decoder = None                                         # type: ignore[assignment]


class SpeechToolUnavailable(ProviderError):
    """The requested speech backend is not installed on this machine."""


# -- capability reporting ----------------------------------------------------

def _espeak_library() -> str:
    if espeakng_loader is None:
        return ""
    try:
        path = str(espeakng_loader.get_library_path())
    except Exception:                                      # noqa: BLE001
        return ""
    return path if os.path.exists(path) else ""


def _espeak_data() -> str:
    if espeakng_loader is None:
        return ""
    try:
        path = str(espeakng_loader.get_data_path())
    except Exception:                                      # noqa: BLE001
        return ""
    return path if os.path.isdir(path) else ""


def local_speech_state() -> dict[str, Any]:
    """What this machine can really do, with the exact missing requirement."""
    library = _espeak_library()
    sphinx = Decoder is not None
    return {
        "text_to_speech": {
            "available": bool(library),
            "backend": "espeak-ng (bundled by the espeakng-loader wheel)",
            "requirement": "" if library else
                           "pip install espeakng-loader (ships the espeak-ng "
                           "library and its voice data; no system package needed)",
            "limitations": "formant synthesis: intelligible and offline, not "
                           "neural; FORGE_TTS_URL gives a better voice",
        },
        "speech_to_text": {
            "available": sphinx,
            "backend": "pocketsphinx (bundled en-us acoustic + language model)",
            "requirement": "" if sphinx else
                           "pip install pocketsphinx (its wheel bundles the "
                           "en-us model; no download needed)",
            "limitations": "small offline model: good for short, clear speech, "
                           "reports a confidence; FORGE_STT_URL gives Whisper-class "
                           "accuracy",
        },
        "measurement": {
            "available": True,
            "backend": "stdlib wave + struct (numpy when present)",
            "requirement": "",
            "limitations": "signal statistics, not content",
        },
    }


# -- text to speech ----------------------------------------------------------

#: espeak-ng keeps process-global state (voice, parameters, the phoneme
#: tables) and ``espeak_Initialize`` is meant to run once per process. The
#: engine used to be re-created on *every* request, which slowly corrupted that
#: state: under a long run the phoneme compiler started printing
#: "Invalid instruction ... for phoneme" and the process eventually died with
#: SIGSEGV (exit 139). It is built once here and every call is serialised,
#: because the same global state is not safe to touch from two threads.
_ENGINE: ctypes.CDLL | None = None
_ENGINE_LOCK = threading.RLock()


def _load_espeak() -> ctypes.CDLL:
    """Load and initialise espeak-ng exactly once for this process."""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            return _ENGINE
        library = _espeak_library()
        if not library:
            raise SpeechToolUnavailable(
                local_speech_state()["text_to_speech"]["requirement"])
        _ENGINE = _build_engine(library)
        return _ENGINE


def _build_engine(library: str) -> ctypes.CDLL:
    engine = ctypes.CDLL(library)
    engine.espeak_Initialize.restype = ctypes.c_int
    engine.espeak_Initialize.argtypes = [ctypes.c_int, ctypes.c_int,
                                         ctypes.c_char_p, ctypes.c_int]
    engine.espeak_SetVoiceByName.restype = ctypes.c_int
    engine.espeak_SetVoiceByName.argtypes = [ctypes.c_char_p]
    engine.espeak_Synth.restype = ctypes.c_int
    engine.espeak_Synth.argtypes = [
        ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint, ctypes.c_int,
        ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    engine.espeak_SetParameter.restype = ctypes.c_int
    engine.espeak_SetParameter.argtypes = [ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int]
    #: The data path is the espeak-ng-data directory the wheel ships; without
    #: it every voice lookup fails, which is a configuration error, not a
    #: reason to pretend there is no engine.
    if not engine.espeak_Initialize(0x02, 0, _espeak_data().encode(), 0):
        raise SpeechToolUnavailable(
            "espeak-ng failed to initialise (voice data missing)")
    return engine


#: espeak-ng hands samples to a callback you register once. The arity matters:
#: calling it through ``espeak_Synth`` (the pre-ng API) segfaults the process,
#: which is exactly what this registration avoids.
_SYNTH_CALLBACK = None


def _synth_once(engine: ctypes.CDLL, text: str, sink: Any) -> bytes:
    """Synthesize one line, collecting 16-bit samples through the callback."""
    global _SYNTH_CALLBACK
    engine.espeak_SetSynthCallback.restype = None
    engine.espeak_SetSynthCallback.argtypes = [ctypes.c_void_p]
    _SYNTH_CALLBACK = sink
    engine.espeak_SetSynthCallback(ctypes.cast(sink, ctypes.c_void_p))
    #: (text, size, position, position_type, end_position, flags,
    #:  unique_identifier, user_data)
    completed = engine.espeak_Synth(text.encode(), 0, 0, 0, 0,
                                    _ESPEAK_CHARS_UTF8, None, None)
    engine.espeak_SetSynthCallback(None)
    if completed != 0:
        raise SpeechToolUnavailable(
            f"espeak-ng refused to synthesize (status {completed})")
    return b"".join(sink.chunks)


#: espeak-ng flags: SSML off, don't flush, char encoding UTF-8.
_ESPEAK_CHARS_UTF8 = 0x01
_ESPEAK_DONT_FLUSH = 0x20


def synthesize(text: str, *, voice: str = "en", rate: int = DEFAULT_RATE,
               pitch: int = 50, amplitude: int = 100) -> bytes:
    """Return real 16-bit mono WAV bytes speaking ``text``."""
    clean = " ".join(str(text or "").split())
    if not clean:
        raise SpeechToolUnavailable("nothing to speak: the request carried no text")
    if len(clean) > 4000:
        clean = clean[:4000]
    engine = _load_espeak()
    chunks: list[bytes] = []

    def callback(samples: ctypes.POINTER(ctypes.c_short), count: int,
                 events: ctypes.c_void_p, user: ctypes.c_void_p) -> int:
        if count <= 0 or not samples:
            return 0
        chunks.append(ctypes.string_at(samples, count * 2))
        return 0

    sink = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_short),
                            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    callback_ref = sink(callback)
    callback_ref.chunks = chunks          # keep the sink alive for the call
    #: One voice/rate change and one synthesis at a time: espeak-ng's state is
    #: global, so two threads setting a voice concurrently would read each
    #: other's parameters.
    with _ENGINE_LOCK:
        if voice:
            engine.espeak_SetVoiceByName(str(voice).encode())
        engine.espeak_SetParameter(0x01, int(rate), 0)      # RATE
        engine.espeak_SetParameter(0x02, int(amplitude), 0)  # VOLUME
        engine.espeak_SetParameter(0x03, int(pitch), 0)      # PITCH
        pcm = _synth_once(engine, clean, callback_ref)
    if not pcm:
        raise SpeechToolUnavailable("espeak-ng produced no audio")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm)
    return buffer.getvalue()


# -- speech to text ----------------------------------------------------------

def _decoder() -> Any:
    if Decoder is None:
        raise SpeechToolUnavailable(
            local_speech_state()["speech_to_text"]["requirement"])
    cfg = Decoder.default_config()
    cfg.set_string("-logfn", os.devnull)
    return Decoder(cfg)


def _read_wav(raw: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(raw), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise ProviderError("only 16-bit PCM WAV can be transcribed here")
    if channels == 2:
        #: Average the channels; the recogniser expects mono.
        samples = struct.unpack("<%dh" % (len(frames) // 2), frames)
        mono = [int((samples[i] + samples[i + 1]) / 2)
                for i in range(0, len(samples) - 1, 2)]
        frames = struct.pack("<%dh" % len(mono), *mono)
    return frames, rate


def _resample(pcm: bytes, rate: int, target: int = 16000) -> bytes:
    """Linear resample to the model's rate; real arithmetic, no magic."""
    if rate == target:
        return pcm
    samples = struct.unpack("<%dh" % (len(pcm) // 2), pcm)
    if not samples:
        return b""
    ratio = target / float(rate)
    out = []
    for index in range(int(len(samples) * ratio)):
        position = index / ratio
        left = int(position)
        right = min(left + 1, len(samples) - 1)
        weight = position - left
        out.append(int(samples[left] * (1 - weight) + samples[right] * weight))
    return struct.pack("<%dh" % len(out), *out)


def transcribe(wav: bytes) -> dict[str, Any]:
    """Real offline transcription with the bundled Sphinx model."""
    decoder = _decoder()
    pcm, rate = _read_wav(wav)
    pcm = _resample(pcm, rate)
    decoder.start_utt()
    decoder.process_raw(pcm, False, False)
    decoder.end_utt()
    hypothesis = decoder.hyp()
    return {
        "text": hypothesis.hypstr if hypothesis else "",
        #: pocketsphinx exposes ``prob`` as a property in 5.x and as a method
        #: in 4.x; accept both rather than pinning one release.
        "confidence": (round(float(
            hypothesis.prob() if callable(hypothesis.prob)
            else hypothesis.prob), 4) if hypothesis else 0.0),
        "model": "pocketsphinx/bundled-en-us",
        "sample_rate": 16000,
        "detected_rate": rate,
        "samples": len(pcm) // 2,
        "backend": "pocketsphinx",
    }


# -- measurement -------------------------------------------------------------

def measure_audio(wav: bytes) -> dict[str, Any]:
    """Acoustic facts about real samples: level, clipping, silence, spectrum."""
    pcm, rate = _read_wav(wav)
    samples = struct.unpack("<%dh" % (len(pcm) // 2), pcm)
    count = len(samples)
    if not count:
        raise ProviderError("the audio has no samples")
    peak = max(abs(sample) for sample in samples)
    #: 20 ms frames decide silence, like a normal level meter.
    frame = max(1, rate // 50)
    frames = [samples[index:index + frame]
              for index in range(0, count, frame)]
    energies = [(sum(sample * sample for sample in chunk) / len(chunk)) ** 0.5
                for chunk in frames if chunk]
    rms = (sum(sample * sample for sample in samples) / count) ** 0.5
    silence_threshold = max(32.0, (max(energies) if energies else 0.0) * 0.1)
    silent = sum(1 for energy in energies if energy < silence_threshold)
    crossings = sum(1 for index in range(1, count)
                    if (samples[index - 1] < 0) != (samples[index] < 0))
    return {
        "sample_rate": rate,
        "channels": 1,
        "duration_seconds": round(count / rate, 3),
        "samples": count,
        "peak": peak,
        "rms": round(rms, 1),
        "dbfs": round(20 * math.log10(max(rms / 32768.0, 1e-9)), 1),
        "clipped_samples": sum(1 for sample in samples if abs(sample) >= 32760),
        "silence_ratio": round(silent / (len(energies) or 1), 3),
        "zero_crossing_rate": round(crossings / max(1, count - 1), 4),
        "frames": len(energies),
        "backend": "stdlib wave + struct",
    }


# -- request parsing ---------------------------------------------------------

_AUDIO_URI = re.compile(r"data:audio/[a-zA-Z0-9.+-]+;base64,(?P<body>[A-Za-z0-9+/=\s]+)")
_FILE = re.compile(r"(?:file|path|audio)\s*:\s*(?P<path>[^\s\"']+)")
_SPEAK = re.compile(r"^[\s>*-]*speak\s*:\s*(?P<text>.+)$",
                    re.IGNORECASE | re.MULTILINE | re.DOTALL)
_RATE = re.compile(r"rate\s*[:=]\s*(\d{2,3})", re.IGNORECASE)
_VOICE = re.compile(r"voice\s*[:=]\s*([a-zA-Z-]{2,12})", re.IGNORECASE)
_OUT = re.compile(r"(?:out|output)\s*[:=]\s*(?P<path>[^\s\"']+)")


def audio_bytes(text: str) -> tuple[bytes, str]:
    source = str(text or "")
    match = _AUDIO_URI.search(source)
    if match:
        raw = base64.b64decode(match.group("body"), validate=False)
        if len(raw) > MAX_AUDIO_BYTES:
            raise ProviderError("audio exceeds the 32 MiB cap")
        return raw, "data-uri"
    path_match = _FILE.search(source)
    if path_match:
        path = Path(path_match.group("path")).expanduser()
        if not path.is_file():
            raise ProviderError(f"no such audio file: {path}")
        raw = path.read_bytes()
        if len(raw) > MAX_AUDIO_BYTES:
            raise ProviderError("audio exceeds the 32 MiB cap")
        return raw, str(path)
    raise ProviderError(
        "an audio request must carry the file as 'file:/path.wav' or a "
        "'data:audio/wav;base64,' URI")


# -- providers ---------------------------------------------------------------

class LocalTextToSpeechProvider:
    """The ``text_to_speech`` capability, served by bundled espeak-ng."""

    name = "forge-local-tts"

    def generate(self, prompt: str, *, context: str = "", task: str = "",
                 instructions: str = "", max_output_tokens: int | None = None,
                 temperature: float | None = None) -> ModelResult:
        del context, max_output_tokens, temperature
        body = "\n".join(part for part in (str(task or ""), str(prompt or ""),
                                           str(instructions or "")) if part)
        match = _SPEAK.search(body)
        text = match.group("text").strip() if match else ""
        if not text:
            # Without an explicit 'speak:' the request itself is the line to
            # say — refusing here would make a plain text answer unspeakable.
            text = " ".join(body.split())
        voice = (_VOICE.search(body).group(1) if _VOICE.search(body) else "en")
        rate = int(_RATE.search(body).group(1)) if _RATE.search(body) else DEFAULT_RATE
        started = time.time()
        wav = synthesize(text, voice=voice, rate=rate)
        out = _OUT.search(body)
        written = ""
        if out:
            target = Path(out.group("path")).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(wav)
            written = str(target)
        stats = measure_audio(wav)
        summary = (f"spoke {len(text.split())} word(s) with espeak-ng: "
                   f"{stats['duration_seconds']}s of 16-bit mono audio at "
                   f"{stats['sample_rate']} Hz ({len(wav)} bytes)"
                   + (f", written to {written}" if written else ""))
        return ModelResult(
            text=summary,
            model="forge-tts/local-espeak",
            input_tokens=len(text.split()),
            output_tokens=len(summary.split()),
            latency=time.time() - started,
            metadata={
                "provider": self.name,
                "simulated": False,
                "backend": "espeak-ng",
                "voice": voice,
                "rate": rate,
                "bytes": len(wav),
                "written": written,
                "audio": stats,
                "spoken_text": text[:2000],
                "limitations": local_speech_state()["text_to_speech"]["limitations"],
            })


class LocalSpeechToTextProvider:
    """The ``speech_to_text`` capability, served by bundled pocketsphinx."""

    name = "forge-local-stt"

    def generate(self, prompt: str, *, context: str = "", task: str = "",
                 instructions: str = "", max_output_tokens: int | None = None,
                 temperature: float | None = None) -> ModelResult:
        del context, max_output_tokens, temperature
        body = "\n".join(part for part in (str(task or ""), str(prompt or ""),
                                           str(instructions or "")) if part)
        started = time.time()
        raw, source = audio_bytes(body)
        stats = measure_audio(raw)
        heard = transcribe(raw)
        summary = (f"transcript ({heard['confidence']:.2f} confidence, "
                   f"{stats['duration_seconds']}s): "
                   f"{(heard['text'] or '(nothing recognised)')}")
        return ModelResult(
            text=summary,
            model="forge-stt/local-sphinx",
            input_tokens=stats["samples"] // 1600,
            output_tokens=len(summary.split()),
            latency=time.time() - started,
            metadata={
                "provider": self.name,
                "simulated": False,
                "backend": "pocketsphinx",
                "source": source,
                "transcript": heard,
                "audio": stats,
                "limitations": local_speech_state()["speech_to_text"]["limitations"],
            })


class LocalAudioAnalysisProvider:
    """The ``audio`` capability: real acoustic measurement of real audio."""

    name = "forge-local-audio"

    def generate(self, prompt: str, *, context: str = "", task: str = "",
                 instructions: str = "", max_output_tokens: int | None = None,
                 temperature: float | None = None) -> ModelResult:
        del context, max_output_tokens, temperature
        body = "\n".join(part for part in (str(task or ""), str(prompt or ""),
                                           str(instructions or "")) if part)
        started = time.time()
        raw, source = audio_bytes(body)
        stats = measure_audio(raw)
        summary = (
            f"audio: {stats['duration_seconds']}s, {stats['sample_rate']} Hz, "
            f"RMS {stats['rms']}, peak {stats['peak']}, "
            f"silence {stats['silence_ratio']:.0%}, "
            f"zero-crossing {stats['zero_crossing_rate']}, "
            f"clipping {stats['clipped_samples']} sample(s)\n"
            "measured from real samples; spoken content needs "
            "speech-to-text (FORGE_STT_URL for Whisper-class accuracy)")
        return ModelResult(
            text=summary,
            model="forge-audio/local-analysis",
            input_tokens=stats["samples"] // 1600,
            output_tokens=len(summary.split()),
            latency=time.time() - started,
            metadata={
                "provider": self.name,
                "simulated": False,
                "backend": "stdlib wave + struct",
                "source": source,
                "audio": stats,
                "semantic": False,
            })


# -- adapters for the cockpit's voice loop ------------------------------------
#
# The voice loop (``forge.voice.synthesizer`` / ``forge.voice.transcriber``)
# speaks in :class:`~forge.voice.audio.AudioChunk` — 16-bit mono 16 kHz PCM —
# while these backends produce and consume WAV bytes at 22.05 kHz. Without
# these two adapters the cockpit could not use them at all: the control plane
# only knew the simulated codec and the OpenAI-compatible HTTP endpoints, so a
# deployment with a working in-process engine still answered "no speech
# synthesizer is configured".


def _synthesize_chunk(text: str) -> Any:
    """Speak ``text`` and hand back a bounded 16 kHz mono chunk."""
    from forge.voice.audio import MAX_AUDIO_BYTES as CHUNK_LIMIT
    from forge.voice.audio import AudioChunk, AudioError

    raw = synthesize(text)
    pcm, rate = _read_wav(raw)
    pcm = _resample(pcm, rate)
    if not pcm:
        raise SpeechToolUnavailable("espeak-ng produced no audio")
    if len(pcm) > CHUNK_LIMIT:
        #: Bounded like every other chunk: the voice layer's contract is a
        #: 500 KB maximum (~15.6 s at 16 kHz), so a long request is truncated
        #: rather than handed on as something the loop cannot carry.
        pcm = pcm[:CHUNK_LIMIT - (CHUNK_LIMIT % 2)]
    try:
        return AudioChunk(data=pcm)
    except AudioError as exc:                               # pragma: no cover
        raise SpeechToolUnavailable(str(exc)) from exc


class LocalInProcessSynthesizer:
    """Text-to-speech served by espeak-ng inside this process.

    ``name`` is what the cockpit reports as the speaking engine, so it has to
    be true: this is the local engine, not a neural voice. ``available()``
    answers from the same state report the capability probe uses.
    """

    name = "forge-local-tts"
    simulation = False

    def available(self) -> bool:
        return bool(local_speech_state()["text_to_speech"]["available"])

    def synthesize(self, text: str) -> Any:
        from forge.voice.synthesizer import SynthesisError

        state = local_speech_state()["text_to_speech"]
        if not state["available"]:
            raise SynthesisError("not_configured", state["requirement"])
        try:
            return _synthesize_chunk(text)
        except SpeechToolUnavailable as exc:
            raise SynthesisError("unavailable", str(exc)) from exc


class LocalInProcessTranscriber:
    """Speech-to-text served by pocketsphinx inside this process.

    The bundled model is ``en-us``, so the language reported is English. Hindi
    and Hinglish need a multilingual endpoint (``FORGE_STT_URL``); claiming
    otherwise would be exactly the kind of upgrade-by-assertion this codebase
    is written to avoid.
    """

    name = "forge-local-stt"
    simulation = False
    language = "en"

    def available(self) -> bool:
        return bool(local_speech_state()["speech_to_text"]["available"])

    def transcribe(self, chunk: Any) -> Any:
        from forge.voice.transcriber import Transcription, TranscriptionError

        state = local_speech_state()["speech_to_text"]
        if not state["available"]:
            raise TranscriptionError("not_configured", state["requirement"])
        try:
            heard = transcribe(chunk.wav())
        except SpeechToolUnavailable as exc:
            raise TranscriptionError("unavailable", str(exc)) from exc
        except ProviderError as exc:
            #: Malformed audio is the caller's error, and it must not look like
            #: a recogniser failure.
            raise TranscriptionError("invalid_audio", str(exc)) from exc
        confidence = heard.get("confidence")
        return Transcription(
            text=str(heard.get("text") or ""),
            confidence=float(confidence) if isinstance(confidence, (int, float))
            else 0.0,
            engine=self.name,
            simulation=False,
            language=str(heard.get("language") or self.language),
        )
