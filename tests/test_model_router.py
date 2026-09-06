from __future__ import annotations

import pytest
from forge.models.provider import MockProvider, OllamaProvider, OpenAIProvider
from forge.models.router import ModelInfo, ModelRouter


def test_model_router_scores_and_routes():
    router = ModelRouter(prefer_local=True)

    local_model = ModelInfo(
        name="local-llama",
        capability="coding",
        available=True,
        is_local=True,
        provider="ollama",
        quality_score=0.9,
    )
    remote_model = ModelInfo(
        name="gpt-4o",
        capability="coding",
        available=True,
        is_local=False,
        provider="openai",
        quality_score=0.95,
        cost_per_1k_tokens=0.01,
    )

    router.register(local_model)
    router.register(remote_model)

    decision = router.route("coding")
    assert decision.selected_model is not None
    assert decision.selected_model.name == "local-llama"
    assert decision.fallback_model is not None
    assert decision.fallback_model.name == "gpt-4o"
    assert len(router.decisions) == 1


def test_historical_stats_update_scoring():
    router = ModelRouter(prefer_local=True)

    m1 = ModelInfo(name="m1", capability="coding", available=True, is_local=True)
    m2 = ModelInfo(name="m2", capability="coding", available=True, is_local=True)

    router.register(m1)
    router.register(m2)

    # Record failures for m1, success for m2
    for _ in range(5):
        router.record_result("m1", success=False, quality=0.2, latency_ms=500.0)
        router.record_result("m2", success=True, quality=0.95, latency_ms=100.0)

    decision = router.route("coding")
    assert decision.selected_model.name == "m2"


def test_mock_provider_generation():
    provider = MockProvider(fixed_response="hello world")
    res = provider.generate("test prompt")
    assert res == "hello world"
    assert len(provider.history) == 1


def test_openai_provider_raises_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAIProvider(api_key=None)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY not configured"):
        provider.generate("prompt")
