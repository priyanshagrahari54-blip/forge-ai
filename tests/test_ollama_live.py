"""Optional live Ollama integration test.

Skipped automatically when no Ollama endpoint is reachable, so the default
suite stays green offline. Set ``FORGE_TEST_OLLAMA_MODEL`` to target a specific
pulled model; otherwise the first model reported by ``/api/tags`` is used.
"""
import os
import socket
from urllib.parse import urlparse

import pytest

from forge.models.fabric import ModelFabric
from forge.models.provider import OllamaProvider
from forge.models.request import ModelRequest


def _endpoint():
    url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 11434
    return host, port


def _reachable():
    host, port = _endpoint()
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no reachable Ollama endpoint")


def test_ollama_live_generate_and_telemetry():
    model_name = os.getenv("FORGE_TEST_OLLAMA_MODEL", "llama3.2")
    provider = OllamaProvider(model=model_name, url=os.getenv("OLLAMA_URL"))

    available = provider.list_models()
    if model_name not in available:
        if not available:
            pytest.skip("no models pulled on the Ollama endpoint")
        model_name = available[0]

    provider = OllamaProvider(model=model_name, url=os.getenv("OLLAMA_URL"))
    fabric = ModelFabric.from_defaults()
    fabric.providers.register("ollama-live", provider)
    from forge.models.registry import Model
    fabric.register_model(Model(
        name=f"ollama-live/{model_name}",
        provider="ollama-live",
        capabilities=("coding", "reasoning", "structured_output"),
        context_window=8192,
        free=True,
        local=True,
    ))

    response = fabric.generate(ModelRequest(
        prompt="Reply with exactly the word: forge",
        capability="coding",
        prefer_local=True,
        prefer_free=True,
    ))
    assert response.success is True
    assert response.text.strip()
    assert fabric.telemetry.count("route") >= 1
    assert fabric.telemetry.count("response") >= 1
