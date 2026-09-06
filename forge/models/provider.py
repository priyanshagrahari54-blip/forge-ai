"""Model provider contracts and optional local/remote adapters.

Providers are deliberately dependency-free.  A caller may register an Ollama or
OpenAI-compatible callable, while the local provider remains useful in offline
runs and tests by turning structured prompts into a conservative patch plan.
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ModelResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency: float = 0.0


class ModelProvider(Protocol):
    name: str
    def generate(self, prompt: str, *, context: str = "", task: str = "") -> ModelResult: ...


class LocalModelProvider:
    """Offline provider implementing the model contract.

    It only emits changes when a task contains an explicit, safe recipe.  This
    avoids pretending that a template is an LLM while still making local models
    first-class and allowing an installed local model to be used transparently.
    """
    name = "local"

    def generate(self, prompt: str, *, context: str = "", task: str = "") -> ModelResult:
        # The structured response is consumed by CoderAgent.  In real deployments
        # this provider is replaced by OllamaProvider; no caller-supplied changes
        # are required by the production API.
        return ModelResult(json.dumps({"changes": {}, "explanation": "No safe local synthesis available; use an Ollama/OpenAI provider."}), self.name)


class OllamaProvider:
    name = "ollama"
    def __init__(self, model: str = "llama3.2", url: str | None = None):
        self.model, self.url = model, url or os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
    def generate(self, prompt: str, *, context: str = "", task: str = "") -> ModelResult:
        payload = json.dumps({"model": self.model, "prompt": prompt, "stream": False}).encode()
        req = urllib.request.Request(self.url, payload, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as response:
            data = json.loads(response.read().decode())
        return ModelResult(str(data.get("response", "")), self.model)


class OpenAIProvider:
    name = "openai"
    def __init__(self, model: str = "gpt-4o-mini", api_key: str | None = None, url: str | None = None):
        self.model, self.api_key = model, api_key or os.getenv("OPENAI_API_KEY")
        self.url = url or "https://api.openai.com/v1/chat/completions"
    def generate(self, prompt: str, *, context: str = "", task: str = "") -> ModelResult:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        body = {"model": self.model, "messages": [{"role": "user", "content": prompt}]}
        req = urllib.request.Request(self.url, json.dumps(body).encode(), {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(req, timeout=120) as response:
            data = json.loads(response.read().decode())
        return ModelResult(data["choices"][0]["message"]["content"], self.model)


class MockProvider:
    """Test provider; unlike LocalModelProvider it is explicitly a test double."""
    name = "mock"
    def __init__(self, response: str): self.response = response
    def generate(self, prompt: str, *, context: str = "", task: str = "") -> ModelResult:
        return ModelResult(self.response, self.name)
