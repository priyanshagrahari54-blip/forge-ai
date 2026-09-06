from __future__ import annotations

import json
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Mapping


class BaseModelProvider(ABC):
    """Abstract base class for model providers."""

    name: str

    @abstractmethod
    def generate(self, prompt: str, system_prompt: str = "", **kwargs: Any) -> str:
        """Generate a response from the model provider."""
        ...


class MockProvider(BaseModelProvider):
    """Deterministic model provider for testing."""

    name = "mock"

    def __init__(
        self,
        fixed_response: str = "mock output",
        custom_responses: Mapping[str, str] | None = None,
    ) -> None:
        self.fixed_response = fixed_response
        self.custom_responses = custom_responses or {}
        self.history: list[dict[str, str]] = []

    def generate(self, prompt: str, system_prompt: str = "", **kwargs: Any) -> str:
        self.history.append({"prompt": prompt, "system_prompt": system_prompt})
        for key, response in self.custom_responses.items():
            if key in prompt or key in system_prompt:
                return response
        return self.fixed_response


class OllamaProvider(BaseModelProvider):
    """Provider for local Ollama LLM inference."""

    name = "ollama"

    def __init__(
        self,
        model_name: str = "llama3",
        base_url: str = "http://localhost:11434",
    ) -> None:
        self.model_name = model_name
        self.base_url = base_url.rstrip("/")

    def generate(self, prompt: str, system_prompt: str = "", **kwargs: Any) -> str:
        url = f"{self.base_url}/api/generate"
        payload = {
            "model": self.model_name,
            "prompt": prompt,
            "system": system_prompt,
            "stream": False,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
                return result.get("response", "")
        except Exception as exc:
            raise RuntimeError(f"Ollama generation failed: {exc}") from exc


class OpenAIProvider(BaseModelProvider):
    """Provider for OpenAI API inference."""

    name = "openai"

    def __init__(
        self,
        model_name: str = "gpt-4o-mini",
        api_key: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.api_key = api_key

    def generate(self, prompt: str, system_prompt: str = "", **kwargs: Any) -> str:
        import os

        key = self.api_key or os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY not configured.")

        url = "https://api.openai.com/v1/chat/completions"
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt or "You are a helpful assistant.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": kwargs.get("temperature", 0.2),
        }
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        }
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
                choices = result.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "")
                return ""
        except Exception as exc:
            raise RuntimeError(f"OpenAI generation failed: {exc}") from exc
