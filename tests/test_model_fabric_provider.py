import pytest

from forge.models.provider import (
    LocalModelProvider,
    MockProvider,
    OllamaProvider,
    ProviderInfo,
    ProviderRegistry,
)


def test_provider_registry_register_and_resolve():
    registry = ProviderRegistry()
    registry.register("local", LocalModelProvider(), ProviderInfo(name="local", kind="fallback"))
    assert registry.has("local")
    assert registry.names() == ["local"]
    assert registry.info("local").kind == "fallback"
    provider = registry.get("local")
    result = provider.generate("hi")
    assert result.model == "local"


def test_duplicate_provider_rejected():
    registry = ProviderRegistry()
    registry.register("p", MockProvider("x"))
    with pytest.raises(ValueError):
        registry.register("p", MockProvider("y"))


def test_provider_must_implement_generate():
    registry = ProviderRegistry()
    with pytest.raises(ValueError):
        registry.register("bad", object())


def test_unknown_provider_raises():
    registry = ProviderRegistry()
    with pytest.raises(KeyError):
        registry.get("missing")


def test_ollama_vision_detection_is_conservative():
    assert OllamaProvider.supports_vision("llava:7b") is True
    assert OllamaProvider.supports_vision("llama3.2-vision") is True
    assert OllamaProvider.supports_vision("llama3.2") is False
    assert OllamaProvider.supports_vision("codellama") is False


def test_local_provider_is_a_safe_noop():
    result = LocalModelProvider().generate("implement a feature")
    assert result.text  # returns structured JSON, not fabricated code
    assert '"changes"' in result.text
