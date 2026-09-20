"""Self-hosted multimodal models inside the Model Fabric.

The fleet's vision/audio specialists need models that genuinely advertise
``vision``, ``speech_to_text`` or ``text_to_speech`` — a text-only model cannot
answer about an image or a recording, and pretending otherwise is exactly the
kind of fake completion Forge must never produce. This module bridges the real
self-hosted providers (:mod:`forge.vision.local`,
:mod:`forge.voice.local_audio`) into the fabric as ordinary providers/models.

Honesty rules:

* a model is registered **only** for a modality whose endpoint is configured,
  so a registry entry always has a real backend behind it;
* a modality that is not configured is reported as a gap with the exact
  environment variable that would enable it — never registered as if it worked;
* the provider raises on failure, so routing/failover and telemetry record a
  real error instead of an empty success;
* file inputs are read only from operator-approved roots, so a prompt cannot
  make the provider read an arbitrary path on the host.
"""
from __future__ import annotations

import base64
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from forge.models.errors import ProviderError
from forge.models.provider import ModelResult

#: Provider name used for every bridged modality.
PROVIDER_NAME = "forge-multimodal"

#: Refuse to read an input file larger than this.
MAX_INPUT_BYTES = 32 * 1024 * 1024

_DATA_URI = re.compile(r"data:(?P<mime>[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+);base64,"
                       r"(?P<payload>[A-Za-z0-9+/=\s]+)")
_PATH_LIKE = re.compile(r"(?:^|\s)(?P<path>(?:/|\./|~)[^\s\"']+)")


@dataclass(frozen=True)
class MultimodalSpec:
    """One configured, real modality backend."""

    kind: str
    capability: str
    url: str
    model: str
    requirements: tuple[str, ...] = ()

    @property
    def fabric_model(self) -> str:
        """Registry name: the kind is the routable identity."""
        return f"{PROVIDER_NAME}/{self.kind}"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "capability": self.capability,
                "model": self.fabric_model, "underlying_model": self.model,
                "url": _safe_url(self.url)}


@dataclass(frozen=True)
class MultimodalGap:
    """A modality that is *not* available, with the exact requirement."""

    kind: str
    capability: str
    reason: str
    requirement: str
    what_is_implemented: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "capability": self.capability,
                "reason": self.reason, "requirement": self.requirement,
                "what_is_implemented": self.what_is_implemented}


#: kind -> (capability, env var for the endpoint, what the endpoint is)
KINDS: dict[str, tuple[str, str, str]] = {
    "vision": ("vision", "FORGE_VISION_URL",
               "an OpenAI-compatible multimodal endpoint serving a "
               "vision-language model (llama.cpp llama-server with a VL "
               "GGUF, vLLM, or Ollama's /v1 layer)"),
    "transcribe": ("speech_to_text", "FORGE_STT_URL",
                   "an OpenAI-compatible /v1/audio/transcriptions endpoint "
                   "(whisper.cpp server, faster-whisper, or vLLM)"),
    "speak": ("text_to_speech", "FORGE_TTS_URL",
              "an OpenAI-compatible /v1/audio/speech endpoint (Piper, "
              "Coqui TTS, or any local TTS server)"),
    "imagine": ("image_generation", "FORGE_IMAGE_URL",
                "an OpenAI-compatible /v1/images/generations endpoint "
                "(LocalAI, ComfyUI/SD-WebUI OpenAI bridge, or any local "
                "diffusion server)"),
}

#: The model id used when the operator did not name one.
DEFAULT_MODELS = {"vision": "vision-model", "transcribe": "whisper",
                  "speak": "tts", "imagine": "image-model"}

_MODEL_ENV = {"vision": "FORGE_VISION_MODEL", "transcribe": "FORGE_STT_MODEL",
              "speak": "FORGE_TTS_MODEL", "imagine": "FORGE_IMAGE_MODEL"}

#: Default image size when the request does not ask for one.
DEFAULT_IMAGE_SIZE = "512x512"

_SIZE_RE = re.compile(r"\bsize=(\d{2,4}x\d{2,4})\b")
_IMAGES_OK_SUFFIX = ".png"


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _safe_url(url: str) -> str:
    """A URL safe to log: credentials and query strings are dropped."""
    text = str(url or "")
    if "@" in text.split("//", 1)[-1].split("/", 1)[0]:
        scheme, rest = text.split("//", 1)
        text = f"{scheme}//{rest.split('@', 1)[1]}"
    return text.split("?", 1)[0].split("#", 1)[0]


def multimodal_specs(env: Mapping[str, str] | None = None
                     ) -> tuple[MultimodalSpec, ...]:
    """The modalities this deployment can really serve, in a stable order."""
    source = _env(env)
    specs: list[MultimodalSpec] = []
    for kind, (capability, var, _) in KINDS.items():
        url = str(source.get(var, "")).strip()
        if not url:
            continue
        specs.append(MultimodalSpec(
            kind, capability, url,
            str(source.get(_MODEL_ENV[kind], "")).strip() or DEFAULT_MODELS[kind],
        ))
    return tuple(specs)


def multimodal_gaps(env: Mapping[str, str] | None = None
                    ) -> tuple[MultimodalGap, ...]:
    """Modalities with no configured endpoint, each with its requirement."""
    source = _env(env)
    gaps: list[MultimodalGap] = []
    for kind, (capability, var, description) in KINDS.items():
        if str(source.get(var, "")).strip():
            continue
        gaps.append(MultimodalGap(
            kind=kind, capability=capability,
            reason=f"no endpoint configured for {capability}",
            requirement=f"set {var} to {description}",
            what_is_implemented=(
                "provider, routing capability, and specialists are implemented "
                "and tested against a real HTTP endpoint; only the endpoint is "
                "missing"),
        ))
    return tuple(gaps)


def _allowed_roots(env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    raw = str(_env(env).get("FORGE_MULTIMODAL_INPUT_DIR", "")).strip()
    roots = [Path(part).expanduser() for part in raw.split(os.pathsep) if part.strip()]
    return tuple(roots) or (Path.cwd(),)


def _read_input_file(prompt: str, env: Mapping[str, str] | None) -> bytes:
    """Read the file a prompt references, but only under approved roots.

    Bounded in size and confined to ``FORGE_MULTIMODAL_INPUT_DIR`` (default:
    the working directory), so a prompt cannot turn the provider into an
    arbitrary file reader on the host.
    """
    match = _PATH_LIKE.search(prompt or "")
    if match is None:
        raise ProviderError(
            "no image/audio input found: pass a data URI or a file path")
    candidate = Path(match.group("path")).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError as exc:                                    # pragma: no cover
        raise ProviderError(f"cannot resolve input path: {exc}") from exc
    roots = _allowed_roots(env)
    if not any(_within(resolved, root) for root in roots):
        raise ProviderError(
            "input path is outside the approved roots "
            f"({', '.join(str(root) for root in roots)}); set "
            "FORGE_MULTIMODAL_INPUT_DIR to allow it")
    if not resolved.is_file():
        raise ProviderError(f"input path is not a file: {resolved}")
    size = resolved.stat().st_size
    if size > MAX_INPUT_BYTES:
        raise ProviderError(f"input file is too large ({size} bytes)")
    return resolved.read_bytes()


def _within(path: Path, root: Path) -> bool:
    """True when *path* sits under *root*.

    ``Path.is_relative_to`` is 3.9+, and this repository supports 3.8
    (``tests/test_python38_compat.py`` enforces it).
    """
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _payload(prompt: str, *, want: str, env: Mapping[str, str] | None) -> bytes:
    """Extract a data URI payload of the requested media type, or a file."""
    for match in _DATA_URI.finditer(prompt or ""):
        mime = match.group("mime").lower()
        if mime.startswith(want):
            raw = re.sub(r"\s+", "", match.group("payload"))
            try:
                return base64.b64decode(raw)
            except Exception as exc:                          # noqa: BLE001
                raise ProviderError(f"malformed base64 payload: {exc}") from exc
    return _read_input_file(prompt, env)


class MultimodalModelProvider:
    """One self-hosted modality, exposed through the provider protocol.

    ``generate`` receives the normal prompt text; the media itself travels as a
    ``data:`` URI inside the prompt or as a path to a file under an approved
    root. The returned :class:`ModelResult` carries the real provider's own
    answer, model name, and structured metadata — never a synthesized one.
    """

    def __init__(self, specs: tuple[MultimodalSpec, ...], *,
                 env: Mapping[str, str] | None = None,
                 timeout: float = 120.0) -> None:
        self._specs = {spec.kind: spec for spec in specs}
        self._env = dict(_env(env))
        self._timeout = float(timeout)

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def generate(self, prompt: str, *, context: str = "", task: str = "",
                 instructions: str = "", max_output_tokens: int | None = None,
                 temperature: float | None = None) -> ModelResult:
        """Dispatch to the modality named at the start of the prompt.

        The registry model name (``forge-multimodal/<kind>``) is what routing
        chose; the executor passes it as the provider prompt, so the kind is
        read from there. A bare prompt without a kind is refused: guessing
        which modality was meant would produce a wrong answer with confidence.
        """
        del context, max_output_tokens, temperature
        text = str(prompt or "")
        kind = ""
        #: The executor passes the specialist's role as ``task``; that is the
        #: authoritative statement of which modality was routed to. The prompt
        #: is only a fallback, because a caller's text may mention a modality
        #: that this request has nothing to do with.
        for candidate, alias_re in self._aliases():
            if alias_re.search(str(task or "")) or alias_re.search(text):
                kind = candidate
                break
        if not kind:
            raise ProviderError(
                "multimodal request must name a configured modality "
                f"({', '.join(sorted(self._specs)) or 'none configured'})")
        #: The media travels in the caller's request text (the executor's
        #: prompt). The fabric uses ``instructions`` for its own routing notes,
        #: so the request text is the primary source and ``instructions`` is
        #: only the fallback — reading the wrong one silently loses the image.
        body = _task_text(text) or _task_text(str(instructions or ""))
        started = time.time()
        if kind == "vision":
            return self._vision(body, started)
        if kind == "transcribe":
            return self._transcribe(body, started)
        if kind == "speak":
            return self._speak(body)
        if kind == "imagine":
            return self._imagine(body, started)
        raise ProviderError(f"unsupported modality {kind!r}")

    def _aliases(self) -> tuple[tuple[str, "re.Pattern[str]"], ...]:
        """kind -> word-boundary matcher for the names callers use.

        Routing chose a model named ``forge-multimodal/<kind>``; the specialist
        that invoked it names the modality either by kind (``vision``) or by
        capability (``speech-to-text``). Both are accepted, so a readable role
        like "speech-to-text specialist" dispatches to the right endpoint.
        """
        aliases: list[tuple[str, re.Pattern[str]]] = []
        for kind, spec in self._specs.items():
            for name in dict.fromkeys((kind, spec.capability)):
                #: Separators are interchangeable: a capability written
                #: ``speech_to_text`` must also match the readable role
                #: "speech-to-text specialist".
                words = r"[-_ ]".join(
                    re.escape(part) for part in re.split(r"[-_ ]", name) if part)
                aliases.append((kind, re.compile(
                    rf"(?<![\w-]){words}(?![\w-])", re.IGNORECASE)))
        return tuple(aliases)

    # -- modalities ----------------------------------------------------------

    def _vision(self, body: str, started: float) -> ModelResult:
        from forge.vision.local import LocalOpenAIVisionProvider

        spec = self._specs["vision"]
        image = _payload(body, want="image/", env=self._env)
        provider = LocalOpenAIVisionProvider(
            model=spec.model, url=spec.url,
            api_key=self._env.get("FORGE_VISION_KEY", ""), timeout=self._timeout)
        result = provider.analyze(image)
        if result.error:
            raise ProviderError(f"vision endpoint failed: {result.error}")
        lines = [result.summary or "no summary returned"]
        for finding in getattr(result, "findings", ()) or ():
            content = str(getattr(finding, "content", "") or "").strip()
            if content:
                lines.append(f"- {getattr(finding, 'kind', 'finding')}: {content}")
        return ModelResult(
            text="\n".join(line for line in lines if line),
            model=result.model or spec.model,
            latency=time.time() - started,
            metadata={
                "kind": "vision",
                "provider": result.provider,
                "simulation": bool(result.simulation),
                "findings": [finding.to_dict() for finding
                             in getattr(result, "findings", ()) or ()],
                "dangerous_instructions": list(
                    getattr(result, "dangerous_instructions", ()) or ()),
            })

    def _transcribe(self, body: str, started: float) -> ModelResult:
        from forge.voice.audio import read_wav
        from forge.voice.local_audio import LocalSpeechToText

        spec = self._specs["transcribe"]
        audio = _payload(body, want="audio/", env=self._env)
        chunk = read_wav(audio)
        provider = LocalSpeechToText(
            model=spec.model, url=spec.url,
            api_key=self._env.get("FORGE_STT_KEY", ""), timeout=self._timeout)
        try:
            transcription = provider.transcribe(chunk)
        except Exception as exc:                              # noqa: BLE001
            raise ProviderError(f"transcription failed: {exc}") from exc
        return ModelResult(
            text=transcription.text,
            model=spec.model,
            latency=time.time() - started,
            metadata={
                "kind": "transcribe",
                "provider": transcription.engine,
                "confidence": transcription.confidence,
                "language": transcription.language,
                "simulation": bool(transcription.simulation),
                "duration_ms": chunk.duration_ms,
            })

    def _speak(self, prompt: str) -> ModelResult:
        """Synthesize speech and write it where the operator allows.

        The prompt names the text to speak and, optionally, an output file
        under ``FORGE_MULTIMODAL_INPUT_DIR``; otherwise the audio is written to
        ``FORGE_TTS_OUTPUT_DIR`` (default: a temp file). The result reports the
        path, duration and sample rate — never an inaudible "success".
        """
        from forge.voice.local_audio import LocalSpeechSynthesizer

        spec = self._specs["speak"]
        text = self._spoken_text(prompt)
        if not text:
            raise ProviderError("nothing to speak: the request carried no text")
        started = time.time()
        provider = LocalSpeechSynthesizer(
            model=spec.model, url=spec.url, voice=self._env.get("FORGE_TTS_VOICE", ""),
            response_format=self._env.get("FORGE_TTS_FORMAT", "wav"),
            api_key=self._env.get("FORGE_TTS_KEY", ""), timeout=self._timeout)
        try:
            chunk = provider.synthesize(text)
        except Exception as exc:                              # noqa: BLE001
            raise ProviderError(f"speech synthesis failed: {exc}") from exc
        target = self._output_path(prompt)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(chunk.wav())
        return ModelResult(
            text=(f"Speech audio written to {target} "
                  f"({chunk.duration_ms / 1000:.2f}s at {chunk.sample_rate} Hz)."),
            model=spec.model,
            latency=time.time() - started,
            metadata={"kind": "speak", "provider": provider.name,
                      "path": str(target), "duration_ms": chunk.duration_ms,
                      "sample_rate": chunk.sample_rate,
                      "simulation": False})

    def _imagine(self, prompt: str, started: float) -> ModelResult:
        """Generate an image through a self-hosted diffusion endpoint.

        Only the endpoint's own host may be fetched when the API answers with
        a URL instead of inline base64, so a hostile response cannot redirect
        the fetch anywhere else. The written file's path is reported; an empty
        answer is an error, never a silent success.
        """
        import json
        import urllib.error
        import urllib.request

        spec = self._specs["imagine"]
        description = str(prompt or "").strip()
        if description.lower().startswith("imagine:"):
            description = description.split(":", 1)[1].strip()
        if not description:
            raise ProviderError("nothing to generate: the request carried no "
                                "description")
        size_match = _SIZE_RE.search(prompt or "")
        size = size_match.group(1) if size_match else DEFAULT_IMAGE_SIZE
        base = spec.url.rstrip("/")
        if base.endswith("/v1"):
            url = f"{base}/images/generations"
        else:
            url = f"{base}/v1/images/generations"
        payload = {"model": spec.model, "prompt": description, "n": 1,
                   "size": size, "response_format": "b64_json"}
        headers = {"Content-Type": "application/json"}
        key = self._env.get("FORGE_IMAGE_KEY", "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                data = json.loads(response.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:                                 # noqa: BLE001
                pass
            raise ProviderError(
                f"image endpoint returned HTTP {exc.code}: {detail}") from exc
        except Exception as exc:                              # noqa: BLE001
            raise ProviderError(f"image endpoint unreachable: {exc}") from exc

        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            raise ProviderError("image endpoint returned no image data")
        first = items[0] if isinstance(items[0], dict) else {}
        raw: bytes | None = None
        if first.get("b64_json"):
            try:
                raw = base64.b64decode(str(first["b64_json"]))
            except Exception as exc:                          # noqa: BLE001
                raise ProviderError(f"malformed image payload: {exc}") from exc
        elif first.get("url"):
            raw = self._fetch_same_host(str(first["url"]), spec.url)
        if not raw:
            raise ProviderError("image endpoint returned neither base64 nor a "
                                "fetchable URL")
        target = self._image_output_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return ModelResult(
            text=(f"Generated image written to {target} "
                  f"({len(raw)} bytes, requested size {size})."),
            model=spec.model,
            latency=time.time() - started,
            metadata={"kind": "imagine", "provider": PROVIDER_NAME,
                      "path": str(target), "bytes": len(raw), "size": size,
                      "simulation": False})

    def _fetch_same_host(self, link: str, endpoint: str) -> bytes:
        """Fetch an image URL only when it points at the configured endpoint."""
        import urllib.parse
        import urllib.request

        target = urllib.parse.urlparse(link)
        allowed = urllib.parse.urlparse(endpoint if "//" in endpoint
                                        else f"//{endpoint}")
        if target.scheme not in ("http", "https"):
            raise ProviderError(f"refusing to fetch {target.scheme!r} URL")
        if (target.hostname or "").lower() != (allowed.hostname or "").lower():
            raise ProviderError(
                "refusing to fetch an image from a host other than the "
                f"configured endpoint ({target.hostname})")
        try:
            with urllib.request.urlopen(
                    link, timeout=self._timeout) as response:
                return response.read(MAX_INPUT_BYTES + 1)[:MAX_INPUT_BYTES]
        except Exception as exc:                              # noqa: BLE001
            raise ProviderError(f"image download failed: {exc}") from exc

    def _image_output_path(self) -> Path:
        directory = str(self._env.get("FORGE_IMAGE_OUTPUT_DIR", "")).strip()
        roots = _allowed_roots(self._env)
        base = (Path(directory).expanduser() if directory
                else roots[0] / "forge-images")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return base / f"image-{stamp}-{os.getpid()}{_IMAGES_OK_SUFFIX}"

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _spoken_text(body: str) -> str:
        """The words to speak: the request itself, with a ``speak:`` marker.

        The executor's role preamble is dropped upstream, so the audio contains
        the caller's words and not the framing text.
        """
        body = str(body or "")
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("speak:"):
                return stripped.split(":", 1)[1].strip()
        cleaned = _PATH_LIKE.sub(" ", body).strip()
        if cleaned.lower().startswith("speak"):
            cleaned = cleaned[len("speak"):].lstrip(": ").strip()
        return cleaned

    def _output_path(self, prompt: str) -> Path:
        directory = str(self._env.get("FORGE_TTS_OUTPUT_DIR", "")).strip()
        roots = _allowed_roots(self._env)
        base = Path(directory).expanduser() if directory else roots[0] / "forge-tts"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return base / f"speech-{stamp}-{os.getpid()}.wav"


def _task_text(prompt: str) -> str:
    """The caller's actual request, not the executor's role preamble.

    Specialist executors front every request with ``You are the ... specialist``
    and a ``Task:`` line; a generator must see the request, not the framing.
    """
    text = str(prompt or "")
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower().startswith("task:"):
            rest = line.split(":", 1)[1].strip()
            remainder = "\n".join(lines[index + 1:]).strip()
            body = "\n".join(part for part in (rest, remainder) if part)
            return body
    return text.strip()


def register_multimodal_models(fabric: Any, *,
                               env: Mapping[str, str] | None = None
                               ) -> dict[str, Any]:
    """Register the configured modalities as real fabric models.

    Returns a report that separates what is now routable from what is missing,
    so a caller can state the exact external requirement instead of claiming
    the capability exists.
    """
    from forge.models.registry import Model

    specs = multimodal_specs(env)
    gaps = multimodal_gaps(env)
    registered: list[str] = []
    if specs:
        provider = MultimodalModelProvider(specs, env=env)
        if not fabric.providers.has(PROVIDER_NAME):
            fabric.register_provider(PROVIDER_NAME, provider)
        for spec in specs:
            if fabric.registry.has(spec.fabric_model):
                registered.append(spec.fabric_model)
                continue
            fabric.register_model(Model(
                name=spec.fabric_model,
                provider=PROVIDER_NAME,
                capabilities=(spec.capability,),
                context_window=8192,
                free=True,
                local=True,
                metadata={
                    "kind": spec.kind,
                    "underlying_model": spec.model,
                    "self_hosted": True,
                    "description": (f"Self-hosted {spec.capability} endpoint "
                                    f"({_safe_url(spec.url)})"),
                    #: Advertised by configuration, not independently probed.
                    "capability_source": "configured-endpoint",
                },
                capability_status={spec.capability: "declared"},
            ))
            registered.append(spec.fabric_model)
    return {
        "schema_version": 1,
        "registered": sorted(registered),
        "available": [spec.to_dict() for spec in specs],
        "gaps": [gap.to_dict() for gap in gaps],
        "note": ("only configured modalities are registered; every registered "
                 "model is backed by a real endpoint, and every unavailable "
                 "modality lists the exact variable that enables it"),
    }


def probe_multimodal(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Ask each configured endpoint a real, tiny request.

    A capability is reported ``verified`` only when the endpoint answered. This
    is an explicit, opt-in network call — never performed at import time.
    """
    from forge.vision.local import LocalOpenAIVisionProvider
    from forge.voice.audio import AudioChunk
    from forge.voice.local_audio import (LocalSpeechToText,
                                         LocalSpeechSynthesizer)

    source = _env(env)
    results: dict[str, Any] = {}
    for spec in multimodal_specs(env):
        entry: dict[str, Any] = {"model": spec.model, "url": _safe_url(spec.url)}
        try:
            if spec.kind == "vision":
                # A 1x1 PNG: the smallest real image an endpoint can answer about.
                png = base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg"
                    "YGCoBwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
                result = LocalOpenAIVisionProvider(
                    model=spec.model, url=spec.url,
                    api_key=source.get("FORGE_VISION_KEY", "")).analyze(png)
                if result.error:
                    raise ProviderError(result.error)
                entry.update(status="verified", detail=(result.summary or "")[:200])
            elif spec.kind == "transcribe":
                silence = AudioChunk(b"\x00\x00" * 1600)   # 100 ms of silence
                transcription = LocalSpeechToText(
                    model=spec.model, url=spec.url,
                    api_key=source.get("FORGE_STT_KEY", "")).transcribe(silence)
                entry.update(status="verified",
                             detail=f"language={transcription.language}")
            elif spec.kind == "speak":
                chunk = LocalSpeechSynthesizer(
                    model=spec.model, url=spec.url,
                    voice=source.get("FORGE_TTS_VOICE", ""),
                    response_format=source.get("FORGE_TTS_FORMAT", "pcm"),
                    api_key=source.get("FORGE_TTS_KEY", "")).synthesize("ok")
                entry.update(status="verified",
                             detail=f"{chunk.duration_ms} ms of audio")
            else:  # imagine: a 1x1 generation is the cheapest real request
                one = MultimodalModelProvider((spec,), env=source)
                result = one.generate("forge-multimodal/imagine\nimagine: a "
                                      "single grey pixel\nsize=64x64")
                entry.update(status="verified",
                             detail=str(result.metadata.get("path", "")))
        except Exception as exc:                              # noqa: BLE001
            entry.update(status="unavailable", error=str(exc)[:300])
        results[spec.kind] = entry
    return {"schema_version": 1, "probed": results,
            "gaps": [gap.to_dict() for gap in multimodal_gaps(env)]}
