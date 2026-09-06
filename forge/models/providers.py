from __future__ import annotations

import os
from dataclasses import dataclass

from forge.models.router import ModelProvider, ModelRequest, ModelResponse


@dataclass(frozen=True)
class MockProvider:
    """A deterministic mock provider for testing and offline mode.

    Always returns a canned response. Useful for tests, demos, and
    environments without API keys.
    """

    name: str = "mock"
    response: str = "mock response"
    capabilities: frozenset[str] = frozenset(
        {"coding", "testing", "debugging", "review", "research", "documentation", "git"}
    )

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            text=self.response,
            model=self.name,
            provider=self.name,
            tokens_used=0,
        )


@dataclass(frozen=True)
class OllamaProvider:
    """Provider for local Ollama models.

    Communicates with a local Ollama server via HTTP. Requires the
    ``OLLAMA_BASE_URL`` environment variable or a custom base URL.
    """

    name: str = "ollama"
    model: str = "llama3"
    base_url: str = ""
    capabilities: frozenset[str] = frozenset(
        {"coding", "testing", "debugging", "review", "research", "documentation", "git"}
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "base_url",
            self.base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        )

    def generate(self, request: ModelRequest) -> ModelResponse:
        try:
            import urllib.request
            import json

            url = f"{self.base_url}/api/generate"
            payload = json.dumps({
                "model": self.model,
                "prompt": request.prompt,
                "stream": False,
                "options": {
                    "temperature": request.temperature,
                    "num_predict": request.max_tokens,
                },
            }).encode("utf-8")

            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return ModelResponse(
                    text=data.get("response", ""),
                    model=self.model,
                    provider=self.name,
                    tokens_used=data.get("eval_count", 0),
                )
        except ImportError:
            return ModelResponse(
                text="",
                model=self.model,
                provider=self.name,
                success=False,
                error="urllib is not available",
            )
        except Exception as exc:
            return ModelResponse(
                text="",
                model=self.model,
                provider=self.name,
                success=False,
                error=str(exc),
            )


@dataclass(frozen=True)
class OpenAIProvider:
    """Provider for OpenAI-compatible APIs.

    Uses the ``OPENAI_API_KEY`` environment variable. Supports custom
    base URLs for OpenAI-compatible endpoints (e.g., Azure, local
    proxies) via ``OPENAI_BASE_URL``.
    """

    name: str = "openai"
    model: str = "gpt-4o-mini"
    api_key: str = ""
    base_url: str = ""
    capabilities: frozenset[str] = frozenset(
        {"coding", "testing", "debugging", "review", "research", "documentation", "git"}
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "api_key",
            self.api_key or os.environ.get("OPENAI_API_KEY", ""),
        )
        object.__setattr__(
            self,
            "base_url",
            self.base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )

    def generate(self, request: ModelRequest) -> ModelResponse:
        if not self.api_key:
            return ModelResponse(
                text="",
                model=self.model,
                provider=self.name,
                success=False,
                error="OPENAI_API_KEY is not set",
            )

        try:
            import urllib.request
            import json

            url = f"{self.base_url}/chat/completions"
            payload = json.dumps({
                "model": self.model,
                "messages": [{"role": "user", "content": request.prompt}],
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
            }).encode("utf-8")

            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                choice = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                return ModelResponse(
                    text=choice,
                    model=self.model,
                    provider=self.name,
                    tokens_used=usage.get("total_tokens", 0),
                )
        except ImportError:
            return ModelResponse(
                text="",
                model=self.model,
                provider=self.name,
                success=False,
                error="urllib is not available",
            )
        except Exception as exc:
            return ModelResponse(
                text="",
                model=self.model,
                provider=self.name,
                success=False,
                error=str(exc),
            )