"""The 100 multimodal specialists, and the honesty rules around them.

A multimodal specialist is only useful if a model really serves its modality.
These tests run the specialists end to end: a real HTTP server stands in for
each self-hosted endpoint, the specialist is selected from the registry, and
its output must be the endpoint's own answer — an image read, a transcript, an
audio file, a generated picture. Modalities with no endpoint are asserted to be
*absent* from the registry and reported with the exact requirement.
"""
from __future__ import annotations

import base64
import io
import json
import struct
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from forge.agents.execution import AgentRequest
from forge.agents.multimodal_fleet import (
    MODALITIES,
    SPECIALIST_CAPABILITY,
    SPECIALIST_NAMES,
    build_multimodal_fleet,
    multimodal_readiness,
)
from forge.core.task_engine import TaskEngine, TaskStatus
from forge.models.errors import ProviderError
from forge.models.fabric import ModelFabric
from forge.models.multimodal_bridge import (
    MultimodalModelProvider,
    multimodal_gaps,
    multimodal_specs,
    probe_multimodal,
    register_multimodal_models,
)
from forge.voice.audio import read_wav

#: A 1x1 PNG — a real, decodable image.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg"
    "YGCoBwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _wav(seconds: float = 0.2, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack(f"<{int(rate * seconds)}h",
                                       *([400] * int(rate * seconds))))
    return buffer.getvalue()


class _Endpoint:
    """A real HTTP server standing in for a self-hosted model endpoint."""

    def __init__(self, *, status: int = 200, body: bytes | dict = b"",
                 content_type: str = "application/json") -> None:
        self.status = status
        self.raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.content_type = content_type
        self.requests: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0") or 0)
                payload = self.rfile.read(length)
                outer.requests.append({
                    "path": self.path, "body": payload,
                    "content_type": self.headers.get("Content-Type", ""),
                    "authorization": self.headers.get("Authorization", ""),
                })
                self.send_response(outer.status)
                self.send_header("Content-Type", outer.content_type)
                self.send_header("Content-Length", str(len(outer.raw)))
                self.end_headers()
                self.wfile.write(outer.raw)

            def log_message(self, *args):
                return

        outer = self
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _chat_answer(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def _vitals(tmp_path, monkeypatch, **env) -> None:
    """Keep every provider's file output inside the test's tmp directory."""
    monkeypatch.setenv("FORGE_MULTIMODAL_INPUT_DIR", str(tmp_path))
    monkeypatch.setenv("FORGE_TTS_OUTPUT_DIR", str(tmp_path / "tts"))
    monkeypatch.setenv("FORGE_IMAGE_OUTPUT_DIR", str(tmp_path / "images"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def _run(fabric, registry, name: str, instructions: str):
    task = TaskEngine().add(f"multimodal-{name}", "multimodal work")
    return registry.get(name).executor.execute(
        AgentRequest(task, TaskStatus.CODING, instructions=instructions))


# -- the fleet ----------------------------------------------------------------

def test_hundred_multimodal_specialists_are_defined():
    assert len(SPECIALIST_NAMES) == 100
    assert len(set(SPECIALIST_NAMES)) == 100      # no duplicates
    per_family = {m.family: m.size for m in MODALITIES}
    assert per_family == {"vision": 25, "image-generation": 25,
                          "speech-to-text": 25, "text-to-speech": 25}
    assert set(SPECIALIST_CAPABILITY.values()) == {
        "vision", "image_generation", "speech_to_text", "text_to_speech"}


def test_no_modality_is_registered_without_a_real_model(tmp_path, monkeypatch):
    _vitals(tmp_path, monkeypatch)
    for var in ("FORGE_VISION_URL", "FORGE_STT_URL", "FORGE_TTS_URL",
                "FORGE_IMAGE_URL"):
        monkeypatch.delenv(var, raising=False)

    fabric = ModelFabric()
    registry, report = build_multimodal_fleet(fabric)

    assert report["defined"] == 100
    assert report["registered"] == 0        # defined is not claimed as working
    assert len(registry) == 0
    assert report["ready_modalities"] == []
    assert set(report["missing_modalities"]) == {
        "vision", "image-generation", "speech-to-text", "text-to-speech"}
    requirements = {row["capability"]: row["requirement"]
                    for row in report["modalities"]}
    assert "FORGE_VISION_URL" in requirements["vision"]
    assert "FORGE_STT_URL" in requirements["speech_to_text"]
    assert "FORGE_TTS_URL" in requirements["text_to_speech"]
    assert "FORGE_IMAGE_URL" in requirements["image_generation"]
    assert all(row["reason"] and row["what_is_implemented"]
               for row in report["modalities"])


def test_a_configured_endpoint_registers_exactly_its_own_family(
        tmp_path, monkeypatch):
    _vitals(tmp_path, monkeypatch)
    monkeypatch.delenv("FORGE_STT_URL", raising=False)
    monkeypatch.delenv("FORGE_TTS_URL", raising=False)
    monkeypatch.delenv("FORGE_IMAGE_URL", raising=False)
    endpoint = _Endpoint(body=_chat_answer("a grey square"))

    fabric = ModelFabric()
    monkeypatch.setenv("FORGE_VISION_URL", endpoint.url)
    register_multimodal_models(fabric)
    registry, report = build_multimodal_fleet(fabric)

    assert report["ready_modalities"] == ["vision"]
    assert report["registered"] == 25
    assert len(registry) == 25
    assert all(name.startswith("vision-") for name in registry.names())
    registration = registry.get("vision-ocr-extraction")
    assert registration.executor.required_capabilities == ("vision",)
    assert registration.executor.model_name == "forge-multimodal/vision"
    endpoint.close()


# -- real work through the fabric ---------------------------------------------

def test_a_vision_specialist_really_reads_an_image(tmp_path, monkeypatch):
    _vitals(tmp_path, monkeypatch)
    for var in ("FORGE_STT_URL", "FORGE_TTS_URL", "FORGE_IMAGE_URL"):
        monkeypatch.delenv(var, raising=False)
    endpoint = _Endpoint(body=_chat_answer("Invoice 42, total Rs 1,299"))
    monkeypatch.setenv("FORGE_VISION_URL", endpoint.url)

    fabric = ModelFabric()
    register_multimodal_models(fabric)
    registry, _ = build_multimodal_fleet(fabric)

    encoded = base64.b64encode(PNG).decode()
    response = _run(fabric, registry, "vision-ocr-extraction",
                    f"Read this receipt: data:image/png;base64,{encoded}")

    assert response.success is True
    assert "Invoice 42" in response.output
    assert response.metadata.get("fleet") is not None
    # The endpoint really received the image, as a data URI, plus our prompt.
    request = json.loads(endpoint.requests[-1]["body"])
    assert endpoint.requests[-1]["path"] == "/v1/chat/completions"
    content = request["messages"][0]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert request["model"] == "vision-model"
    endpoint.close()


def test_a_speech_to_text_specialist_really_transcribes_audio(
        tmp_path, monkeypatch):
    _vitals(tmp_path, monkeypatch)
    for var in ("FORGE_VISION_URL", "FORGE_TTS_URL", "FORGE_IMAGE_URL"):
        monkeypatch.delenv(var, raising=False)
    endpoint = _Endpoint(body={"text": "kal ki meeting das baje hai",
                               "language": "hi",
                               "segments": [{"avg_logprob": -0.105}]})
    monkeypatch.setenv("FORGE_STT_URL", endpoint.url)

    fabric = ModelFabric()
    register_multimodal_models(fabric)
    registry, _ = build_multimodal_fleet(fabric)

    encoded = base64.b64encode(_wav()).decode()
    response = _run(fabric, registry, "speech-to-text-hindi-asr",
                    f"Transcribe this: data:audio/wav;base64,{encoded}")

    assert response.success is True
    assert response.output == "kal ki meeting das baje hai"
    assert response.metadata.get("confidence") is not None
    assert endpoint.requests[-1]["path"] == "/v1/audio/transcriptions"
    assert b"multipart/form-data" in endpoint.requests[-1]["content_type"].encode()
    endpoint.close()


def test_a_text_to_speech_specialist_writes_real_audio(tmp_path, monkeypatch):
    _vitals(tmp_path, monkeypatch)
    for var in ("FORGE_VISION_URL", "FORGE_STT_URL", "FORGE_IMAGE_URL"):
        monkeypatch.delenv(var, raising=False)
    pcm = struct.pack("<3200h", *([900] * 3200))       # 200 ms at 16 kHz
    endpoint = _Endpoint(body=pcm, content_type="audio/pcm")
    monkeypatch.setenv("FORGE_TTS_URL", endpoint.url)
    monkeypatch.setenv("FORGE_TTS_FORMAT", "pcm")

    fabric = ModelFabric()
    register_multimodal_models(fabric)
    registry, _ = build_multimodal_fleet(fabric)

    response = _run(fabric, registry, "text-to-speech-hindi-tts",
                    "speak: Standup complete, two tasks waiting for approval.")

    assert response.success is True
    chunk = read_wav(open(response.metadata["path"], "rb").read())
    assert chunk.duration_ms == 200
    assert chunk.sample_rate == 16000
    request = json.loads(endpoint.requests[-1]["body"])
    assert request["input"].startswith("Standup complete")
    assert "specialist on the Forge" not in request["input"]   # no preamble
    endpoint.close()


def test_an_image_generation_specialist_writes_the_generated_image(
        tmp_path, monkeypatch):
    _vitals(tmp_path, monkeypatch)
    for var in ("FORGE_VISION_URL", "FORGE_STT_URL", "FORGE_TTS_URL"):
        monkeypatch.delenv(var, raising=False)
    endpoint = _Endpoint(body={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
    monkeypatch.setenv("FORGE_IMAGE_URL", endpoint.url)

    fabric = ModelFabric()
    register_multimodal_models(fabric)
    registry, _ = build_multimodal_fleet(fabric)

    response = _run(fabric, registry, "image-generation-icon-set",
                    "Generate a 64x64 app icon, flat style, size=64x64")

    assert response.success is True
    written = open(response.metadata["path"], "rb").read()
    assert written == PNG                       # exactly what the endpoint sent
    request = json.loads(endpoint.requests[-1]["body"])
    assert endpoint.requests[-1]["path"] == "/v1/images/generations"
    assert request["size"] == "64x64"
    assert "app icon" in request["prompt"]
    endpoint.close()


def test_a_failing_endpoint_is_an_error_never_an_empty_success(
        tmp_path, monkeypatch):
    _vitals(tmp_path, monkeypatch)
    for var in ("FORGE_STT_URL", "FORGE_TTS_URL", "FORGE_IMAGE_URL"):
        monkeypatch.delenv(var, raising=False)
    endpoint = _Endpoint(status=500, body=b'{"error":"model crashed"}')
    monkeypatch.setenv("FORGE_VISION_URL", endpoint.url)

    fabric = ModelFabric()
    register_multimodal_models(fabric)
    registry, _ = build_multimodal_fleet(fabric)

    encoded = base64.b64encode(PNG).decode()
    response = _run(fabric, registry, "vision-visual-qa",
                    f"check data:image/png;base64,{encoded}")

    assert response.success is False
    assert "500" in (response.error + response.output)
    assert response.output.strip() == "" or "500" in response.output
    endpoint.close()


# -- the bridge's own guard rails ---------------------------------------------

def test_input_paths_outside_the_approved_roots_are_refused(
        tmp_path, monkeypatch):
    _vitals(tmp_path, monkeypatch)
    spec = multimodal_specs({"FORGE_VISION_URL": "http://127.0.0.1:1"})[0]
    provider = MultimodalModelProvider((spec,), env={
        "FORGE_VISION_URL": "http://127.0.0.1:1",
        "FORGE_MULTIMODAL_INPUT_DIR": str(tmp_path),
    })
    with pytest.raises(ProviderError) as error:
        provider.generate("vision: read /etc/passwd")
    assert "approved roots" in str(error.value)

    # A file inside the root is read (and the unreachable endpoint is the only
    # thing that then fails — no silent success).
    target = tmp_path / "screenshot.png"
    target.write_bytes(PNG)
    with pytest.raises(ProviderError):
        provider.generate(f"vision: read {target}")


def test_an_unconfigured_modality_cannot_be_called(tmp_path, monkeypatch):
    _vitals(tmp_path, monkeypatch)
    spec = multimodal_specs({"FORGE_VISION_URL": "http://127.0.0.1:1"})[0]
    provider = MultimodalModelProvider((spec,), env={})
    assert provider.kinds() == ("vision",)
    with pytest.raises(ProviderError) as error:
        provider.generate("speak: hello")
    assert "must name a configured modality" in str(error.value)


def test_probe_reports_verified_only_when_the_endpoint_answers(
        tmp_path, monkeypatch):
    _vitals(tmp_path, monkeypatch)
    endpoint = _Endpoint(body=_chat_answer("a 1x1 grey pixel"))
    try:
        verified = probe_multimodal({"FORGE_VISION_URL": endpoint.url})
        assert verified["probed"]["vision"]["status"] == "verified"
    finally:
        endpoint.close()

    unreachable = probe_multimodal({"FORGE_VISION_URL": "http://127.0.0.1:1"})
    assert unreachable["probed"]["vision"]["status"] == "unavailable"
    assert unreachable["probed"]["vision"]["error"]
    # Modalities with no endpoint are never "verified": they are gaps.
    assert {gap["capability"] for gap in unreachable["gaps"]} >= {
        "speech_to_text", "text_to_speech", "image_generation"}


def test_readiness_names_the_exact_requirement_per_modality():
    fabric = ModelFabric()
    reports = multimodal_readiness(fabric, env={})
    by_capability = {report.capability: report for report in reports}
    assert all(report.status == "MISSING" for report in reports)
    assert all(report.defined == 25 and report.registered == 0
               for report in reports)
    env_vars = {"vision": "FORGE_VISION_URL",
                "speech_to_text": "FORGE_STT_URL",
                "text_to_speech": "FORGE_TTS_URL",
                "image_generation": "FORGE_IMAGE_URL"}
    for capability, variable in env_vars.items():
        assert variable in by_capability[capability].requirement
    assert {gap.capability for gap in multimodal_gaps({})} == set(env_vars)

# -- wiring into the running system -------------------------------------------

def test_the_control_plane_registers_a_configured_multimodal_endpoint(
        tmp_path, monkeypatch):
    """The interactive plane must see the same modalities the fleet does."""
    from helpers_a34 import make_plane

    _vitals(tmp_path, monkeypatch)
    endpoint = _Endpoint(body=_chat_answer("a dashboard with two charts"))
    try:
        monkeypatch.setenv("FORGE_VISION_URL", endpoint.url)
        plane = make_plane(tmp_path, start=False)
        assert plane.multimodal_report["registered"] == ["forge-multimodal/vision"]
        readiness = {report.family: report
                     for report in multimodal_readiness(plane.fabric)}
        assert readiness["vision"].status == "READY"
        assert readiness["vision"].registered == 25
        #: A modality that has a *verified local backend* is READY and the
        #: report names the model that serves it; one that has neither a local
        #: backend nor an endpoint is MISSING and the report names the endpoint
        #: that would enable it. Which of the two happens for text-to-speech
        #: depends on this machine, so both branches are asserted here rather
        #: than hard-coding a status that a working backend would falsify.
        speech = readiness["text-to-speech"]
        local = [name for name in speech.models
                 if name.startswith("forge-local/")]
        if local:
            assert speech.status == "READY"
            assert speech.registered == 25
            assert "forge-local/text-to-speech" in speech.models
            #: The gap text stays in the report even when a local backend
            #: serves the modality: it is the upgrade path to a bigger model,
            #: not a claim that something is missing.
            assert "FORGE_TTS_URL" in speech.requirement
        else:
            assert speech.status == "MISSING"
            assert "FORGE_TTS_URL" in speech.requirement
        #: And in either case: READY may never be claimed without a model.
        for report in readiness.values():
            if report.status == "READY":
                assert report.models and report.registered == report.defined
    finally:
        endpoint.close()


def test_the_multimodal_endpoint_is_read_only_and_authenticated(tmp_path):
    from forge.api import routes_multimodal
    from helpers_a34 import make_client, make_plane

    routes = {route.path: route.methods for route in routes_multimodal.router.routes}
    assert routes == {"/multimodal": {"GET"}}

    plane = make_plane(tmp_path)
    client = make_client(plane)
    response = client.get("/api/v1/multimodal")
    assert response.status_code == 401          # auth is not bypassed
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"
