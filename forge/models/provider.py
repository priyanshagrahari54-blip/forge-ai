"""Provider contracts, registries, and optional local/remote model adapters."""
from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ModelResult:
    """Result returned by a concrete provider's ``generate`` call."""

    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency: float = 0.0


class ModelProvider(Protocol):
    """Minimal provider contract: a name and a synchronous ``generate``."""

    name: str

    def generate(self, prompt: str, *, context: str = "", task: str = "") -> ModelResult:
        ...


#: The fabric uses the terms interchangeably; providers are exactly the things
#: that turn a prompt into a ``ModelResult``.
Provider = ModelProvider


@dataclass(frozen=True)
class ProviderInfo:
    """Declarative metadata describing a registered provider."""

    name: str
    display_name: str = ""
    kind: str = "local"  # local | remote | fallback | mock
    local: bool = True
    free: bool = True
    capabilities: tuple[str, ...] = ()
    endpoint: str = ""  # never contains credentials
    model: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name or self.name,
            "kind": self.kind,
            "local": self.local,
            "free": self.free,
            "capabilities": list(self.capabilities),
            "endpoint": self.endpoint,
            "model": self.model,
        }


class ProviderRegistry:
    """Central registry of provider instances by name.

    Providers are resolved by name from the model registry entries, so routing
    logic never touches provider internals until a decision has been made.
    """

    def __init__(self, providers: dict[str, Provider] | None = None) -> None:
        self._providers: dict[str, Provider] = {}
        self._info: dict[str, ProviderInfo] = {}
        for name, provider in (providers or {}).items():
            self.register(name, provider)

    def register(self, name: str, provider: Provider, info: ProviderInfo | None = None) -> None:
        if not name:
            raise ValueError("Provider name cannot be empty")
        if not hasattr(provider, "generate") or not callable(getattr(provider, "generate", None)):
            raise ValueError(f"Provider {name!r} must implement generate()")
        if name in self._providers:
            raise ValueError(f"Provider already registered: {name}")
        self._providers[name] = provider
        self._info[name] = info or ProviderInfo(name=name)

    def get(self, name: str) -> Provider:
        try:
            return self._providers[name]
        except KeyError:
            raise KeyError(f"Unknown provider: {name}") from None

    def info(self, name: str) -> ProviderInfo:
        return self._info.get(name, ProviderInfo(name=name))

    def has(self, name: str) -> bool:
        return name in self._providers

    def names(self) -> list[str]:
        return sorted(self._providers)

    def items(self) -> list[tuple[str, Provider]]:
        return [(name, self._providers[name]) for name in self.names()]

    def snapshot(self) -> list[dict[str, Any]]:
        return [self.info(name).to_dict() for name in self.names()]

    def __len__(self) -> int:
        return len(self._providers)


class LocalModelProvider:
    """Safe offline fallback; it refuses arbitrary synthesis instead of faking it."""

    name = "local"

    def generate(self, prompt: str, *, context: str = "", task: str = "") -> ModelResult:
        started = time.perf_counter()
        text = json.dumps({
            "changes": {},
            "explanation": "No safe local synthesis engine is configured; use Ollama or another provider.",
        })
        return ModelResult(text, self.name, latency=time.perf_counter() - started)


class OllamaProvider:
    """First-class local Ollama provider.

    Talks to the Ollama HTTP API. Requires no credentials. The endpoint and
    model are configurable; nothing here reaches the network at import time.
    """

    name = "ollama"

    #: Conservative list of model families Ollama serves that accept images.
    VISION_MODEL_PREFIXES: tuple[str, ...] = (
        "llava",
        "bakllava",
        "moondream",
        "minicpm-v",
        "qwen2.5vl",
        "qwen-vl",
        "llama3.2-vision",
        "gemma3",
    )

    def __init__(self, model: str = "llama3.2", url: str | None = None):
        self.model = model
        self.url = url or os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")

    def generate(self, prompt: str, *, context: str = "", task: str = "") -> ModelResult:
        started = time.perf_counter()
        payload = json.dumps({"model": self.model, "prompt": prompt, "stream": False}).encode()
        request = urllib.request.Request(self.url, payload, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = json.loads(response.read().decode())
        except Exception as exc:
            raise RuntimeError(f"Ollama model {self.model!r} unavailable at {self.url}: {exc}") from exc
        return ModelResult(str(data.get("response", "")), self.model,
                           latency=time.perf_counter() - started)

    @classmethod
    def supports_vision(cls, model: str) -> bool:
        """Best-effort detection of vision-capable Ollama models.

        Deliberately conservative: only well-known multimodal families are
        recognized. Unknown models are assumed text-only rather than having
        vision support fabricated.
        """
        lowered = model.lower()
        return any(lowered.startswith(prefix) for prefix in cls.VISION_MODEL_PREFIXES)

    def _tags_url(self) -> str:
        return self.url.replace("/api/generate", "/api/tags")

    def list_models(self) -> list[str]:
        """Return the model names currently served by the Ollama endpoint."""
        try:
            with urllib.request.urlopen(self._tags_url(), timeout=10) as response:
                data = json.loads(response.read().decode())
        except Exception as exc:
            raise RuntimeError(f"Ollama endpoint unavailable at {self.url}: {exc}") from exc
        return sorted(
            str(item.get("name", ""))
            for item in data.get("models", [])
            if item.get("name")
        )

    def health(self) -> dict[str, Any]:
        """Probe availability. Raises ``RuntimeError`` when unreachable."""
        return {"available": True, "models": self.list_models()}


class OpenAIProvider:
    """Optional remote provider, enabled only when a key is configured.

    This adapter is intentionally inert without ``OPENAI_API_KEY``. Forge never
    fabricates quotas or auth, and never logs the key.
    """

    name = "openai"

    def __init__(self, model: str = "gpt-4o-mini", api_key: str | None = None,
                 url: str | None = None):
        self.model = model
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.url = url or "https://api.openai.com/v1/chat/completions"

    def generate(self, prompt: str, *, context: str = "", task: str = "") -> ModelResult:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        started = time.perf_counter()
        body = {"model": self.model, "messages": [{"role": "user", "content": prompt}]}
        request = urllib.request.Request(
            self.url, json.dumps(body).encode(),
            {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = json.loads(response.read().decode())
        except Exception as exc:
            raise RuntimeError(f"OpenAI model {self.model!r} request failed: {exc}") from exc
        return ModelResult(data["choices"][0]["message"]["content"], self.model,
                           latency=time.perf_counter() - started)


class MockProvider:
    """Explicit test double; production callers should use a real provider."""

    name = "mock"

    def __init__(self, response: str):
        self.response = response

    def generate(self, prompt: str, *, context: str = "", task: str = "") -> ModelResult:
        return ModelResult(self.response, self.name)
