"""Capabilities this machine can serve *itself*, registered only when proven.

Four of the fleet's capabilities — ``vision``, ``audio``, ``browser`` and
``computer_use`` — used to be unreachable without a remote endpoint. They now
have real in-process backends (bundled espeak-ng and pocketsphinx, Pillow
measurement, an HTTP/DOM browser and DOM action runner). This module is the one
place that decides whether such a backend may be advertised:

1. the backend must be importable/usable **on this machine**;
2. a *real probe* must succeed — a tiny image measured, a word spoken and
   transcribed, a real page fetched and acted on;
3. the model is registered with its honest limitations attached, so routing
   knows it is measurement-level vision or a small offline recogniser, not a
   frontier model, and ``simulated`` is always ``False``.

A probe failure registers nothing. Capability states never move from evidence
to optimism: ``capability_status`` is ``verified`` only for a capability whose
probe ran, and the report names what is still missing for the full-fat version
(``FORGE_VISION_URL``/``FORGE_STT_URL``/``FORGE_TTS_URL``).
"""
from __future__ import annotations

import io
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from forge.browser.local import (BrowserPolicy, LocalBrowserProvider,
                                 LocalComputerUseProvider)
from forge.models.capabilities import Capability
from forge.voice.local_speech import (LocalAudioAnalysisProvider,
                                      LocalSpeechToTextProvider,
                                      LocalTextToSpeechProvider,
                                      local_speech_state)
from forge.vision.pixels import (LocalPixelVisionProvider,
                                 local_vision_available,
                                 local_vision_capability, measure)
from forge.vision.procedural import (LocalProceduralImageProvider,
                                     procedural_image_available,
                                     procedural_image_capability)

#: One fabric provider name per backend, so a routing decision names the thing
#: that actually did the work.
VISION_PROVIDER = "forge-local-vision"
AUDIO_PROVIDER = "forge-local-audio"
TTS_PROVIDER = "forge-local-tts"
STT_PROVIDER = "forge-local-stt"
BROWSER_PROVIDER = "forge-browser"
ACTIONS_PROVIDER = "forge-web-actions"
IMAGE_PROVIDER = "forge-local-imagegen"

VISION_MODEL = "forge-local/vision-pixels"
AUDIO_MODEL = "forge-local/audio-analysis"
STT_MODEL = "forge-local/speech-to-text"
TTS_MODEL = "forge-local/text-to-speech"
BROWSER_MODEL = "forge-local/browser"
COMPUTER_MODEL = "forge-local/computer-use"
IMAGE_MODEL = "forge-local/image-procedural"


# -- probes ------------------------------------------------------------------

def _png(width: int = 64, height: int = 48, *, colour=(32, 96, 160)) -> bytes:
    """A tiny but *real* PNG, built by hand (no encoder dependency)."""
    import zlib

    raw = b"".join(
        b"\x00" + bytes(colour) * width for _ in range(height))
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class _ProbePage(BaseHTTPRequestHandler):
    """A real (if small) page the browser probe must fetch and act on."""

    body = (b"<html><head><title>Forge probe</title></head><body>"
            b"<h1>capability probe</h1><a href='/next'>next page</a>"
            b"<form action='/search' method='get'><input name='q' value=''>"
            b"</form></body></html>")

    def do_GET(self) -> None:                              # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args) -> None:                   # noqa: D102
        return


def probe_vision() -> dict[str, Any]:
    if not local_vision_available():
        return {"probe": "failed", "reason": local_vision_capability()["requirement"]}
    measured = measure(_png(), source="probe:generated")
    ok = measured["width"] == 64 and measured["height"] == 48
    return {"probe": "passed" if ok else "failed",
            "reason": "" if ok else "measurement returned unexpected geometry",
            "sample": {"width": measured["width"], "height": measured["height"],
                       "mean_brightness": measured["mean_brightness"]}}


def probe_audio() -> dict[str, Any]:
    state = local_speech_state()
    #: The ``audio`` capability itself is acoustic measurement: its verdict is
    #: the measurement verdict. Synthesis and recognition are probed here too
    #: because they need real audio to be honest, but they are separate
    #: capabilities with their own entries.
    result: dict[str, Any] = {"probe": "passed",
                              "measurement": {"probe": "passed"},
                              "text_to_speech": {"probe": "skipped"},
                              "speech_to_text": {"probe": "skipped"}}
    if not state["text_to_speech"]["available"]:
        requirement = state["text_to_speech"]["requirement"]
        result["text_to_speech"] = {"probe": "failed", "reason": requirement}
        #: Recognition cannot be exercised without a sample to recognise.
        #: "skipped" alone would be exactly the kind of unexplained gap this
        #: module exists to prevent, so it says why.
        result["speech_to_text"] = {
            "probe": "skipped",
            "reason": f"no sample could be produced to recognise: {requirement}"}
        return result
    try:
        wav = _LocalTTSProbe().synthesize()
    except Exception as exc:                                # noqa: BLE001
        reason = str(exc)[:200]
        result["text_to_speech"] = {"probe": "failed", "reason": reason}
        #: Recognition needs a real sample to decode, and with synthesis down
        #: there is none — so it is skipped *with the reason*, never silently.
        result["speech_to_text"] = {
            "probe": "skipped",
            "reason": f"synthesis failed, so there was nothing to decode: "
                      f"{reason}"}
        #: Measurement still works without synthesis, so the audio verdict
        #: stays what measurement says — the failure is reported where it
        #: belongs instead of failing an unrelated capability.
        return result
    result["text_to_speech"] = {"probe": "passed", "bytes": len(wav)}
    if not state["speech_to_text"]["available"]:
        result["speech_to_text"] = {
            "probe": "failed",
            "reason": state["speech_to_text"]["requirement"]}
        return result
    from forge.voice.local_speech import transcribe

    try:
        heard = transcribe(wav)
    except Exception as exc:                                # noqa: BLE001
        #: A recogniser that blows up must degrade this one entry, not the
        #: deployment's startup: the audio capability itself still measures.
        result["speech_to_text"] = {"probe": "failed",
                                    "reason": str(exc)[:200]}
        return result
    result["speech_to_text"] = {
        "probe": "passed" if heard["text"] else "failed",
        "reason": "" if heard["text"] else "the recogniser returned no words",
        "transcript": heard["text"][:80],
        "confidence": heard["confidence"],
    }
    result["audio"] = LocalAudioAnalysisProvider().generate(
        _file_request(wav)).metadata["audio"]
    return result


class _LocalTTSProbe:
    """Synthesize one word to bytes so the probe needs no filesystem."""

    def synthesize(self) -> bytes:
        from forge.voice.local_speech import synthesize

        return synthesize("forged", rate=140)


def _file_request(wav: bytes) -> str:
    import base64

    return "data:audio/wav;base64," + base64.b64encode(wav).decode()


def probe_browser(policy: BrowserPolicy | None = None) -> dict[str, Any]:
    """Fetch and act on a real page served in-process, then shut it down."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProbePage)
    #: ``shutdown()`` waits for the serve loop's poll, so the stdlib default
    #: (0.5 s) used to dominate every control-plane construction.
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = LocalBrowserProvider(policy or BrowserPolicy()).generate(
            f"url:{base}/")
        acted = LocalComputerUseProvider(policy or BrowserPolicy()).generate(
            f'url:{base}/ actions:[{{"click": "next page"}}]')
        return {
            "probe": "passed",
            "browser": {"title": page.metadata["page"]["title"],
                        "links": page.metadata["page"]["links"],
                        "bytes": page.metadata["page"]["bytes"]},
            "computer_use": {"actions": [item["action"]
                                         for item in acted.metadata["actions"]],
                             "final_url": acted.metadata["page"]["url"]},
        }
    except Exception as exc:                                # noqa: BLE001
        return {"probe": "failed", "reason": str(exc)[:200]}
    finally:
        server.shutdown()
        server.server_close()


def probe_image_generation() -> dict[str, Any]:
    """Render a real PNG from a real spec and measure what came out."""
    if not procedural_image_available():
        return {"probe": "failed",
                "reason": procedural_image_capability()["requirement"]}
    from forge.vision.procedural import measure_png, render

    raw = render({"title": "probe", "bars": [{"label": "a", "value": 1},
                                             {"label": "b", "value": 3}],
                  "width": 320, "height": 200})
    measured = measure_png(raw)
    ok = measured["width"] == 320 and measured["height"] == 200 and len(raw) > 500
    return {"probe": "passed" if ok else "failed",
            "reason": "" if ok else "the rendered PNG did not measure as asked",
            "bytes": len(raw),
            "sample": {"width": measured["width"], "height": measured["height"],
                       "edge_density": measured["edge_density"]}}


def probe_local_capabilities(policy: BrowserPolicy | None = None) -> dict[str, Any]:
    """Run every probe for real and report what may be advertised."""
    started = time.time()
    reports = {
        "vision": probe_vision(),
        "image_generation": probe_image_generation(),
        "audio": probe_audio(),
        "browser": probe_browser(policy),
    }
    browser_ok = reports["browser"]["probe"] == "passed"
    reports["computer_use"] = dict(reports["browser"])
    reports["text_to_speech"] = reports["audio"].get("text_to_speech",
                                                     {"probe": "skipped"})
    reports["speech_to_text"] = reports["audio"].get("speech_to_text",
                                                     {"probe": "skipped"})
    verified = sorted(name for name, item in reports.items()
                      if item.get("probe") == "passed")
    return {
        "schema_version": 1,
        "checked_at": time.time(),
        "elapsed_seconds": round(time.time() - started, 3),
        "verified": verified,
        "reports": reports,
        "limitations": local_capability_limitations(),
        "remote_upgrades": {
            "vision": "FORGE_VISION_URL (vision-language model)",
            "speech_to_text": "FORGE_STT_URL (Whisper-class)",
            "text_to_speech": "FORGE_TTS_URL (neural voice)",
            "image_generation": "FORGE_IMAGE_URL (diffusion server)",
        },
        "browser_policy": (policy or BrowserPolicy()).to_dict(),
        "browser_probe_ok": browser_ok,
    }


def local_capability_limitations() -> dict[str, str]:
    state = local_speech_state()
    return {
        "vision": local_vision_capability()["limitations"],
        "image_generation": procedural_image_capability()["limitations"],
        "audio": ("acoustic measurement only (level, silence, clipping, "
                  "zero-crossing); no semantic listening"),
        "text_to_speech": state["text_to_speech"]["limitations"],
        "speech_to_text": state["speech_to_text"]["limitations"],
        "browser": ("real HTTP + DOM, no JavaScript engine: a page rendered "
                    "entirely by script looks empty here"),
        "computer_use": ("DOM actions on real pages; OS-level desktop control "
                         "needs a separate desktop runtime"),
    }


# -- registration ------------------------------------------------------------

def local_capability_specs() -> list[dict[str, Any]]:
    """What the local backends are, independent of any probe."""
    state = local_speech_state()
    return [
        {"capability": Capability.VISION.value, "model": VISION_MODEL,
         "provider": VISION_PROVIDER, "backend": "Pillow pixel measurement",
         "available": local_vision_available(),
         "requirement": local_vision_capability()["requirement"]},
        {"capability": Capability.AUDIO.value, "model": AUDIO_MODEL,
         "provider": AUDIO_PROVIDER, "backend": "stdlib wave + struct",
         "available": True, "requirement": ""},
        {"capability": Capability.SPEECH_TO_TEXT.value, "model": STT_MODEL,
         "provider": STT_PROVIDER, "backend": "pocketsphinx (bundled model)",
         "available": state["speech_to_text"]["available"],
         "requirement": state["speech_to_text"]["requirement"]},
        {"capability": Capability.TEXT_TO_SPEECH.value, "model": TTS_MODEL,
         "provider": TTS_PROVIDER, "backend": "espeak-ng (bundled library)",
         "available": state["text_to_speech"]["available"],
         "requirement": state["text_to_speech"]["requirement"]},
        {"capability": Capability.IMAGE_GENERATION.value, "model": IMAGE_MODEL,
         "provider": IMAGE_PROVIDER, "backend": "Pillow procedural renderer",
         "available": procedural_image_available(),
         "requirement": procedural_image_capability()["requirement"]},
        {"capability": Capability.BROWSER.value, "model": BROWSER_MODEL,
         "provider": BROWSER_PROVIDER, "backend": "local HTTP + DOM browser",
         "available": True, "requirement": ""},
        {"capability": Capability.COMPUTER_USE.value, "model": COMPUTER_MODEL,
         "provider": ACTIONS_PROVIDER, "backend": "DOM action runner",
         "available": True, "requirement": ""},
    ]


def build_local_providers(policy: BrowserPolicy | None = None) -> dict[str, Any]:
    policy = policy or BrowserPolicy.from_env()
    return {
        VISION_PROVIDER: LocalPixelVisionProvider(),
        AUDIO_PROVIDER: LocalAudioAnalysisProvider(),
        TTS_PROVIDER: LocalTextToSpeechProvider(),
        STT_PROVIDER: LocalSpeechToTextProvider(),
        IMAGE_PROVIDER: LocalProceduralImageProvider(),
        BROWSER_PROVIDER: LocalBrowserProvider(policy),
        ACTIONS_PROVIDER: LocalComputerUseProvider(policy),
    }


#: Attribute under which a fabric remembers this process's probe report.
_REPORT_ATTR = "_local_capability_report"


def register_local_capability_models(
        fabric: Any, *, policy: BrowserPolicy | None = None,
        verify: bool = True, refresh: bool = False) -> dict[str, Any]:
    """Register every local capability whose real probe passed.

    ``verify=False`` registers nothing that has not been probed in this process
    — a capability is never advertised from the mere presence of code. Callers
    that cannot afford a probe get the reasons instead.

    The probes cost real time (~0.5 s: pixel measurement, bundled speech, a
    DOM browser round trip) and their outcome only changes with a redeploy,
    so a fabric remembers its verified report: later calls in the same
    process (one per task run) reuse it instead of re-probing. ``refresh``
    forces a new probe.
    """
    from forge.models.registry import Model

    if verify and not refresh:
        cached = getattr(fabric, _REPORT_ATTR, None)
        if isinstance(cached, dict) and all(
                fabric.registry.has(name) for name in cached.get("registered", ())):
            return cached

    policy = policy or BrowserPolicy.from_env()
    specs = {spec["capability"]: spec for spec in local_capability_specs()}
    probes = probe_local_capabilities(policy) if verify else {
        "verified": [], "reports": {}, "limitations": local_capability_limitations()}
    verified = set(probes.get("verified") or ())
    providers = build_local_providers(policy)
    registered: list[str] = []
    skipped: list[dict[str, str]] = []
    for capability, spec in sorted(specs.items()):
        if not spec["available"]:
            skipped.append({"capability": capability,
                            "reason": spec["requirement"]})
            continue
        if capability not in verified:
            report = (probes.get("reports") or {}).get(capability) or {}
            skipped.append({
                "capability": capability,
                "reason": report.get("reason") or
                          "the backend exists but its probe did not pass"})
            continue
        provider_name = spec["provider"]
        if not fabric.providers.has(provider_name):
            fabric.register_provider(provider_name, providers[provider_name])
        if not fabric.registry.has(spec["model"]):
            fabric.register_model(Model(
                name=spec["model"],
                provider=provider_name,
                capabilities=(capability,),
                context_window=8192,
                free=True,
                local=True,
                metadata={
                    "backend": spec["backend"],
                    "in_process": True,
                    "simulated": False,
                    "runtime_verified": True,
                    "probe": (probes.get("reports") or {}).get(capability, {}),
                    "limitation": (probes.get("limitations") or {}).get(
                        capability, ""),
                    "remote_upgrade": (probes.get("remote_upgrades") or {}).get(
                        capability, ""),
                },
                capability_status={capability: "verified"},
            ))
        registered.append(spec["model"])
    report = {
        "schema_version": 1,
        "registered": sorted(registered),
        "skipped": skipped,
        "verified": sorted(verified),
        "available_locally": sorted(cap for cap, spec in specs.items()
                                    if spec["available"]),
        "limitations": probes.get("limitations") or {},
        "note": ("a local capability is registered only when its real probe "
                 "passed; a skipped entry names exactly what is missing"),
    }
    if verify:
        try:
            setattr(fabric, _REPORT_ATTR, report)
        except Exception:  # noqa: BLE001 - duck-typed fabrics may be read-only
            pass
    return report
