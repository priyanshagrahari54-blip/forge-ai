from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from forge.models.providers import MockProvider, OllamaProvider, OpenAIProvider
from forge.models.router import (
    ModelInfo,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelRouter,
)


class FailingProvider:
    """A provider that always fails, for testing fallback behavior."""

    name: str = "failing"
    capabilities: frozenset[str] = frozenset({"coding", "review"})

    def __init__(self, name: str = "failing", error: str = "API error") -> None:
        self.name = name
        self.error = error
        self._raise = False

    def generate(self, request: ModelRequest) -> ModelResponse:
        if self._raise:
            raise RuntimeError(self.error)
        return ModelResponse(
            text="",
            model=self.name,
            provider=self.name,
            success=False,
            error=self.error,
        )


class TestModelRequest:
    def test_default_values(self):
        request = ModelRequest(prompt="test", capability="coding")

        assert request.prompt == "test"
        assert request.capability == "coding"
        assert request.max_tokens == 1000
        assert request.temperature == 0.0
        assert request.metadata == {}

    def test_custom_values(self):
        request = ModelRequest(
            prompt="test",
            capability="review",
            max_tokens=500,
            temperature=0.5,
            metadata={"key": "value"},
        )

        assert request.max_tokens == 500
        assert request.temperature == 0.5
        assert request.metadata == {"key": "value"}


class TestModelResponse:
    def test_default_success(self):
        response = ModelResponse(text="output", model="test", provider="mock")

        assert response.success is True
        assert response.error == ""
        assert response.tokens_used == 0

    def test_failure_response(self):
        response = ModelResponse(
            text="",
            model="test",
            provider="mock",
            success=False,
            error="API key missing",
        )

        assert response.success is False
        assert response.error == "API key missing"


class TestMockProvider:
    def test_name(self):
        provider = MockProvider()

        assert provider.name == "mock"

    def test_capabilities(self):
        provider = MockProvider()

        assert "coding" in provider.capabilities
        assert "git" in provider.capabilities

    def test_generate_returns_canned_response(self):
        provider = MockProvider(response="test output")
        request = ModelRequest(prompt="hello", capability="coding")

        response = provider.generate(request)

        assert response.success is True
        assert response.text == "test output"
        assert response.model == "mock"
        assert response.provider == "mock"

    def test_custom_capabilities(self):
        provider = MockProvider(
            capabilities=frozenset({"coding", "review"})
        )

        assert "coding" in provider.capabilities
        assert "review" in provider.capabilities


class TestModelRouter:
    def test_register_provider(self):
        router = ModelRouter()
        provider = MockProvider()

        router.register(provider)

        assert len(router.providers) == 1
        assert router.providers[0] is provider

    def test_add_provider_alias(self):
        router = ModelRouter()
        provider = MockProvider()

        router.add_provider(provider)

        assert len(router.providers) == 1

    def test_providers_for_capability(self):
        router = ModelRouter()
        coding_provider = MockProvider(
            name="coding-only",
            capabilities=frozenset({"coding"}),
        )
        review_provider = MockProvider(
            name="review-only",
            capabilities=frozenset({"review"}),
        )

        router.register(coding_provider)
        router.register(review_provider)

        coding = router.providers_for("coding")
        review = router.providers_for("review")

        assert len(coding) == 1
        assert coding[0].name == "coding-only"
        assert len(review) == 1
        assert review[0].name == "review-only"

    def test_providers_for_no_match(self):
        router = ModelRouter()
        provider = MockProvider(capabilities=frozenset({"coding"}))

        router.register(provider)

        assert router.providers_for("nonexistent") == []

    def test_route_success(self):
        router = ModelRouter()
        provider = MockProvider(response="routed output")

        router.register(provider)

        request = ModelRequest(prompt="test", capability="coding")
        response = router.route(request)

        assert response.success is True
        assert response.text == "routed output"
        assert response.provider == "mock"

    def test_route_fallback(self):
        router = ModelRouter()
        failing = FailingProvider(name="failing", error="API error")
        succeeding = MockProvider(name="succeeding", response="success")

        router.register(failing)
        router.register(succeeding)

        request = ModelRequest(prompt="test", capability="coding")
        response = router.route(request)

        assert response.success is True
        assert response.text == "success"
        assert response.provider == "succeeding"

    def test_route_no_providers(self):
        router = ModelRouter()

        request = ModelRequest(prompt="test", capability="coding")
        response = router.route(request)

        assert response.success is False
        assert "No provider available" in response.error

    def test_route_all_fail(self):
        router = ModelRouter()
        p1 = FailingProvider(name="p1", error="err1")
        p2 = FailingProvider(name="p2", error="err2")

        router.register(p1)
        router.register(p2)

        request = ModelRequest(prompt="test", capability="coding")
        response = router.route(request)

        assert response.success is False
        assert "All providers failed" in response.error
        assert "p1" in response.error
        assert "p2" in response.error

    def test_route_handles_exception(self):
        router = ModelRouter()
        exploding = FailingProvider(name="exploding", error="boom")
        exploding._raise = True
        succeeding = MockProvider(name="succeeding", response="ok")

        router.register(exploding)
        router.register(succeeding)

        request = ModelRequest(prompt="test", capability="coding")
        response = router.route(request)

        assert response.success is True
        assert response.text == "ok"

    def test_route_respects_availability(self):
        router = ModelRouter(availability=lambda p: p.name != "offline")
        online = MockProvider(name="online", response="online result")
        offline = MockProvider(name="offline", response="offline result")

        router.register(online)
        router.register(offline)

        request = ModelRequest(prompt="test", capability="coding")
        response = router.route(request)

        assert response.success is True
        assert response.text == "online result"

    def test_select_returns_model_info(self):
        router = ModelRouter()
        provider = MockProvider(name="selected")

        router.register(provider)

        info = router.select("coding")

        assert info is not None
        assert info.name == "selected"
        assert info.capability == "coding"
        assert info.available is True

    def test_select_no_match(self):
        router = ModelRouter()

        info = router.select("nonexistent")

        assert info is None


class TestOllamaProvider:
    def test_default_name(self):
        provider = OllamaProvider()

        assert provider.name == "ollama"

    def test_capabilities_include_coding(self):
        provider = OllamaProvider()

        assert "coding" in provider.capabilities

    def test_default_base_url(self):
        provider = OllamaProvider()

        assert provider.base_url == "http://localhost:11434"

    def test_custom_base_url(self):
        provider = OllamaProvider(base_url="http://custom:8080")

        assert provider.base_url == "http://custom:8080"

    def test_base_url_from_env(self):
        with patch.dict("os.environ", {"OLLAMA_BASE_URL": "http://env:11434"}):
            provider = OllamaProvider()

            assert provider.base_url == "http://env:11434"


class TestOpenAIProvider:
    def test_default_name(self):
        provider = OpenAIProvider()

        assert provider.name == "openai"

    def test_capabilities_include_coding(self):
        provider = OpenAIProvider()

        assert "coding" in provider.capabilities

    def test_missing_api_key_returns_error(self):
        with patch.dict("os.environ", {}, clear=True):
            provider = OpenAIProvider(api_key="")
            request = ModelRequest(prompt="test", capability="coding")

            response = provider.generate(request)

            assert response.success is False
            assert "OPENAI_API_KEY" in response.error

    def test_default_base_url(self):
        provider = OpenAIProvider()

        assert provider.base_url == "https://api.openai.com/v1"

    def test_custom_base_url(self):
        provider = OpenAIProvider(base_url="https://custom.api.com/v1")

        assert provider.base_url == "https://custom.api.com/v1"