"""Provider payload propagation tests (A31 hardening).

Prove that task, repository context, and routing/generation constraints are
actually incorporated into the model invocation — not silently dropped at the
provider boundary — for the production providers, and that the fabric forwards
only the keyword arguments each provider declares.
"""
import json
from unittest import mock

import pytest

from forge.models.fabric import ModelFabric
from forge.models.provider import (
    LocalModelProvider,
    OllamaProvider,
    OpenAIProvider,
    ProviderRegistry,
    ModelResult,
    compose_provider_prompt,
)
from forge.models.registry import Model, ModelRegistry
from forge.models.request import ModelRequest


# -- composition helper ----------------------------------------------------

def test_compose_provider_prompt_preserves_all_sections():
    composed = compose_provider_prompt(
        "do the thing",
        context="file: app.py",
        task="add CSV export",
        instructions="required capabilities: coding",
    )
    assert "TASK\nadd CSV export" in composed
    assert "INSTRUCTIONS\ndo the thing" in composed
    assert "REPOSITORY CONTEXT\nfile: app.py" in composed
    assert "CONSTRAINTS\nrequired capabilities: coding" in composed


def test_compose_provider_prompt_skips_empty_sections():
    assert compose_provider_prompt("", context="  ", task="", instructions="") == ""
    assert "TASK" not in compose_provider_prompt("just prompt")


def test_request_constraints_text_includes_capability_and_limits():
    request = ModelRequest(
        prompt="p", capability="coding", required_capabilities=("coding", "structured_output"),
        min_context_window=4096, max_output_tokens=256, max_latency_ms=5000,
        prefer_free=True, prefer_local=True,
    )
    text = request.constraints_text()
    assert "required capabilities: coding, structured_output" in text
    assert "minimum context window: 4096" in text
    assert "maximum output tokens: 256" in text
    assert "maximum latency: 5000 ms" in text
    assert "prefer free provider" in text
    assert "prefer local provider" in text


# -- Ollama payload ---------------------------------------------------------

class _GenerateResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self._body


def _capture_generate(urlopen_mock, response_bytes, captured):
    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode())
        return _GenerateResponse(response_bytes)

    urlopen_mock.side_effect = fake_urlopen


def test_ollama_payload_contains_task_and_context():
    captured = {}
    with mock.patch("forge.models.provider.urllib.request.urlopen") as urlopen:
        _capture_generate(urlopen, json.dumps({"response": "ok"}).encode(), captured)
        OllamaProvider(model="llama3.2").generate(
            "implement it", context="file: app.py", task="add CSV export",
        )

    body = captured["body"]
    assert captured["url"].endswith("/api/generate")
    assert body["system"] == "add CSV export"          # task in the native system slot
    assert "REPOSITORY CONTEXT" in body["prompt"]       # context reaches the prompt
    assert "file: app.py" in body["prompt"]
    assert "implement it" in body["prompt"]
    assert body["stream"] is False


def test_ollama_payload_native_options():
    captured = {}
    with mock.patch("forge.models.provider.urllib.request.urlopen") as urlopen:
        _capture_generate(urlopen, json.dumps({"response": "ok"}).encode(), captured)
        OllamaProvider(model="llama3.2").generate(
            "p", context="c", task="t", instructions="constraint", max_output_tokens=128, temperature=0.3,
        )

    body = captured["body"]
    assert "CONSTRAINTS" in body["prompt"]
    assert body["options"]["num_predict"] == 128
    assert body["options"]["temperature"] == 0.3


def test_ollama_url_normalizes_bare_host():
    assert OllamaProvider(url="http://127.0.0.1:11434").url == "http://127.0.0.1:11434/api/generate"
    assert OllamaProvider(url="http://127.0.0.1:11434/").url == "http://127.0.0.1:11434/api/generate"
    assert OllamaProvider(url="http://127.0.0.1:11434/api/generate").url == "http://127.0.0.1:11434/api/generate"


# -- OpenAI payload ---------------------------------------------------------

def test_openai_payload_uses_native_messages_structure():
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data.decode())
        assert "Authorization" in request.headers
        assert "Bearer test-key" == request.headers["Authorization"]
        return _GenerateResponse(json.dumps(
            {"choices": [{"message": {"content": "done"}}]}
        ).encode())

    with mock.patch("forge.models.provider.urllib.request.urlopen", side_effect=fake_urlopen):
        result = OpenAIProvider(api_key="test-key", model="gpt-4o-mini").generate(
            "implement it", context="file: app.py", task="add CSV export",
            instructions="required capabilities: coding", max_output_tokens=64, temperature=0.2,
        )

    assert result.text == "done"
    body = captured["body"]
    assert body["model"] == "gpt-4o-mini"
    assert body["messages"][0] == {"role": "system", "content": "add CSV export"}
    user = body["messages"][1]["content"]
    assert user.startswith("INSTRUCTIONS\nimplement it")
    assert "REPOSITORY CONTEXT\nfile: app.py" in user
    assert "CONSTRAINTS\nrequired capabilities: coding" in user
    assert body["max_tokens"] == 64
    assert body["temperature"] == 0.2


def test_openai_requires_api_key():
    with pytest.raises(RuntimeError):
        OpenAIProvider(api_key=None).generate("p", task="t", context="c")


# -- Local provider ---------------------------------------------------------

def test_local_provider_refuses_synthesis_regardless_of_request():
    result = LocalModelProvider().generate(
        "implement a login", context="file: app.py", task="add login", instructions="constraints",
    )
    assert result.model == "local"
    data = json.loads(result.text)
    assert data["changes"] == {}
    assert "Ollama" in data["explanation"]
    # Task/context are never turned into fabricated code.
    assert "login" not in data["explanation"].lower()


# -- Fabric → provider forwarding -------------------------------------------

def test_fabric_forwards_full_request_to_accepting_provider():
    received = {}

    class Recording:
        name = "rec"

        def generate(self, prompt, *, context="", task="", instructions="",
                     max_output_tokens=None, temperature=None):
            received.update(prompt=prompt, context=context, task=task,
                            instructions=instructions, max_output_tokens=max_output_tokens,
                            temperature=temperature)
            return ModelResult("ok", self.name)

    fabric = ModelFabric(
        registry=ModelRegistry([Model(name="rec/m", provider="rec", capabilities=("coding",))]),
        providers=ProviderRegistry({"rec": Recording()}),
    )
    response = fabric.generate(ModelRequest(
        prompt="p", capability="coding", task="the task", context="the context",
        max_output_tokens=99, temperature=0.25,
    ))
    assert response.success
    assert received["prompt"] == "p"
    assert received["task"] == "the task"
    assert received["context"] == "the context"
    assert "required capabilities: coding" in received["instructions"]
    assert received["max_output_tokens"] == 99
    assert received["temperature"] == 0.25


def test_fabric_never_passes_unsupported_kwargs():
    class Narrow:
        name = "narrow"

        def generate(self, prompt, *, context="", task=""):
            return ModelResult("ok", self.name)

    fabric = ModelFabric(
        registry=ModelRegistry([Model(name="narrow/m", provider="narrow", capabilities=("coding",))]),
        providers=ProviderRegistry({"narrow": Narrow()}),
    )
    response = fabric.generate(ModelRequest(
        prompt="p", capability="coding", task="t", context="c", max_output_tokens=10,
    ))
    assert response.success
    assert response.text == "ok"
