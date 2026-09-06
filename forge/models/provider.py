"""Provider contracts and optional local/remote model adapters."""
from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ModelResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency: float = 0.0


class ModelProvider(Protocol):
    name: str

    def generate(self, prompt: str, *, context: str = "", task: str = "") -> ModelResult:
        ...


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
    name = "ollama"

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


class OpenAIProvider:
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
