"""A self-hosted, OpenAI-compatible model endpoint is a first-class provider.

These tests talk to a real HTTP server (no mocking of urllib), because the
whole point of the provider is that it reaches an actual local runtime:
llama.cpp's ``llama-server``, vLLM, Ollama's ``/v1``, LM Studio. They also pin
the honesty rules: configuration alone never marks a model runtime-verified,
and a capability the operator did not declare is never advertised.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from forge.models.capabilities import TEXT_CAPABILITIES
from forge.models.config import FabricConfig
from forge.models.fabric import ModelFabric
from forge.models.local_openai import LocalOpenAIProvider


class _Handler(BaseHTTPRequestHandler):
    model_id = "local-test-model"
    #: llama.cpp's llama-server reports the model file path as the model id.
    reported_id = "local-test-model"
    requests: list[dict] = []
    fail_generate = False

    def log_message(self, *args):  # keep test output clean
        return

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._send({"object": "list", "data": [
                {"id": self.reported_id, "object": "model"}]})
            return
        self._send({"error": "not found"}, status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else "{}"
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        type(self).requests.append({"path": self.path, "body": body})
        if type(self).fail_generate:
            self._send({"error": {"message": "model failed to load"}},
                       status=500)
            return
        self._send({
            "choices": [{"message": {"role": "assistant",
                                     "content": "real local answer"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 4},
        })


@pytest.fixture()
def local_server():
    _Handler.requests = []
    _Handler.fail_generate = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_generate_reaches_the_local_endpoint_and_parses_usage(local_server):
    provider = LocalOpenAIProvider(_Handler.model_id, local_server)
    result = provider.generate("write a function", task="coding",
                               max_output_tokens=32, temperature=0.2)
    assert result.text == "real local answer"
    assert result.model == _Handler.model_id
    assert result.output_tokens == 4
    sent = _Handler.requests[-1]
    assert sent["path"].endswith("/v1/chat/completions")
    assert sent["body"]["model"] == _Handler.model_id
    assert sent["body"]["max_tokens"] == 32
    assert sent["body"]["temperature"] == 0.2


def test_list_models_is_a_real_probe(local_server):
    provider = LocalOpenAIProvider(_Handler.model_id, local_server)
    assert provider.list_models() == [_Handler.model_id]
    assert provider.health()["available"] is True


def test_provider_never_invents_a_capability_it_was_not_configured_for():
    provider = LocalOpenAIProvider("m", "http://127.0.0.1:9",
                                   capabilities=("coding",))
    assert provider.capabilities == ("coding",)


def test_unreachable_endpoint_raises_instead_of_reporting_success():
    provider = LocalOpenAIProvider("m", "http://127.0.0.1:9",
                                   timeout=1.0)
    with pytest.raises(RuntimeError):
        provider.generate("hello")
    with pytest.raises(RuntimeError):
        provider.list_models()


def test_endpoint_failure_is_a_real_error_not_an_empty_answer(local_server):
    _Handler.fail_generate = True
    provider = LocalOpenAIProvider(_Handler.model_id, local_server)
    with pytest.raises(RuntimeError) as excinfo:
        provider.generate("hello")
    assert "HTTP 500" in str(excinfo.value)


def test_config_enables_the_provider_only_with_url_and_model():
    empty = FabricConfig.from_dict({}, env={})
    assert empty.local_openai_enabled is False

    configured = FabricConfig.from_dict({}, env={
        "FORGE_LOCAL_MODEL_URL": "http://127.0.0.1:8080",
        "FORGE_LOCAL_MODEL_NAME": "gemma-3-270m-q4_k_m.gguf",
        "FORGE_LOCAL_MODEL_CAPABILITIES": "coding, reasoning, documentation",
        "FORGE_LOCAL_MODEL_CONTEXT": "8192",
    })
    assert configured.local_openai_enabled is True
    assert configured.local_openai_url == "http://127.0.0.1:8080"
    assert configured.local_openai_capabilities == (
        "coding", "reasoning", "documentation")
    assert configured.local_openai_context_window == 8192


def test_fabric_registers_the_local_model_without_claiming_verification():
    config = FabricConfig.from_dict({}, env={})
    config.local_openai_enabled = True
    config.local_openai_url = "http://127.0.0.1:8080"
    config.local_openai_model = "local-test-model"
    fabric = ModelFabric.from_defaults(config)

    assert fabric.providers.has("local-openai")
    info = fabric.providers.info("local-openai")
    # A self-hosted runtime is local and free; it is not a cloud provider.
    assert info.local is True and info.free is True
    model = fabric.registry.get("local-test-model")
    assert model.local is True
    assert model.capabilities == TEXT_CAPABILITIES
    # Configuration is not verification.
    assert model.metadata.get("runtime_verified") is False


def test_runtime_monitor_promotes_the_model_only_after_a_real_probe(
        local_server, tmp_path):
    from forge.models.configured_runtime_bridge import sync_configured_runtimes
    from forge.models.runtime_monitor_service import RuntimeMonitorService

    config = FabricConfig.from_dict({}, env={})
    config.local_openai_enabled = True
    config.local_openai_url = local_server
    config.local_openai_model = _Handler.model_id
    fabric = ModelFabric.from_defaults(config)
    sync_configured_runtimes(fabric)

    service = RuntimeMonitorService(
        fabric, state_path=tmp_path / "monitor.json", interval_seconds=0.05)
    payload = service.tick(force=True)
    by_provider = {entry["provider"]: entry for entry in payload["results"]}
    # The self-hosted endpoint is probed for real and reaches LIVE; the
    # Ollama runtime in the same fabric stays UNAVAILABLE because nothing is
    # listening — the probe never marks an unreachable model live.
    assert by_provider["local-openai"]["state"] == "LIVE", payload["results"]
    assert by_provider["ollama"]["state"] == "UNAVAILABLE"
    model = fabric.registry.get(_Handler.model_id)
    assert model.metadata["runtime_verified"] is True
    assert model.available is True


def test_a_self_hosted_model_actually_serves_generation_through_the_fabric(
        local_server):
    """End to end: route a request and get real text back from the endpoint."""
    config = FabricConfig.from_dict({}, env={})
    config.local_openai_enabled = True
    config.local_openai_url = local_server
    config.local_openai_model = _Handler.model_id
    fabric = ModelFabric.from_defaults(config)
    model = fabric.registry.get(_Handler.model_id)
    model.metadata["runtime_verified"] = True

    from forge.models.request import ModelRequest
    response = fabric.generate(ModelRequest(
        prompt="write a function", capability="coding",
        required_capabilities=("coding",), caller="test"))
    assert response.success is True, response.error
    assert response.text == "real local answer"
    assert response.model == _Handler.model_id
    assert response.provider == "local-openai"


def test_probe_matches_a_model_reported_by_file_path(local_server):
    """llama-server reports the GGUF path; the configured name must match it.

    Otherwise a correctly running local model is reported "not_found" by the
    runtime probe and stays out of routing.
    """
    _Handler.reported_id = "/models/weights/local-test-model"
    try:
        provider = LocalOpenAIProvider(_Handler.model_id, local_server)
        listed = provider.list_models()
        assert _Handler.model_id in listed, listed
        assert "/models/weights/local-test-model" in listed
        assert provider.health()["available"] is True
    finally:
        _Handler.reported_id = _Handler.model_id


def test_routing_constraints_are_not_sent_as_chat_text_by_default():
    """Routing/generation constraints belong to the router, not the chat.

    The provider receives the rendered constraint block from the engine. On a
    real local instruct model that block dominated the prompt and was echoed
    back verbatim instead of an answer. The applicable limits are carried by
    the API's native fields, and the block itself is recorded on the result so
    it is not silently dropped.
    """
    seen: dict = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            seen["body"] = json.loads(self.rfile.read(length).decode())
            payload = json.dumps({"choices": [
                {"message": {"content": "ok"}, "finish_reason": "stop"}]})
            body = payload.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        constraints = "required capabilities: coding\nmaximum output tokens: 48"
        provider = LocalOpenAIProvider("model.gguf", url)

        result = provider.generate("write the handler", task="backend",
                                   instructions=constraints,
                                   max_output_tokens=48)

        text = seen["body"]["messages"][-1]["content"]
        assert "maximum output tokens" not in text
        assert "CONSTRAINTS" not in text
        assert "write the handler" in text
        assert seen["body"]["max_tokens"] == 48          # native field carries it
        assert result.metadata["routing_constraints"] == constraints

        # Opt-in keeps the old behaviour for endpoints that expect the block.
        seen.clear()
        explicit = LocalOpenAIProvider("model.gguf", url, send_constraints=True)
        explicit.generate("write the handler", instructions=constraints)
        assert "CONSTRAINTS" in seen["body"]["messages"][-1]["content"]
    finally:
        server.shutdown()
        server.server_close()
