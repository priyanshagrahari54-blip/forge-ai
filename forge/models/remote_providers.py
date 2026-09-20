"""Stdlib remote provider adapters for common hosted model APIs."""
from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any

from forge.models.provider import ModelResult, compose_provider_prompt


class HostedProvider:
    """Small, dependency-free hosted-provider adapter with bounded HTTP."""

    def __init__(self, name: str, model: str, api_key: str, endpoint: str,
                 *, timeout: float = 120.0) -> None:
        self.name = name
        self.model = model
        self.api_key = api_key
        self.endpoint = endpoint.rstrip("/")
        self.timeout = max(5.0, min(float(timeout), 600.0))

    def _post(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            url, json.dumps(body).encode("utf-8"),
            {"Content-Type": "application/json", **headers},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise RuntimeError(f"{self.name} response exceeded 4 MiB limit")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError(f"{self.name} returned a non-object response")
        return data


class OpenAICompatibleProvider(HostedProvider):
    """OpenAI-compatible chat-completions provider (OpenRouter/Groq)."""

    def list_models(self) -> list[str]:
        request = urllib.request.Request(
            self.endpoint + "/models",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        return sorted(str(x.get("id")) for x in data.get("data", [])
                      if isinstance(x, dict) and x.get("id"))

    def generate(self, prompt: str, *, context: str = "", task: str = "",
                 instructions: str = "", max_output_tokens: int | None = None,
                 temperature: float | None = None) -> ModelResult:
        started = time.perf_counter()
        messages: list[dict[str, str]] = []
        if task.strip():
            messages.append({"role": "system", "content": task.strip()})
        messages.append({"role": "user", "content": compose_provider_prompt(
            prompt, context=context, instructions=instructions)})
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        if max_output_tokens is not None:
            body["max_tokens"] = int(max_output_tokens)
        if temperature is not None:
            body["temperature"] = float(temperature)
        data = self._post(self.endpoint + "/chat/completions", body,
                          {"Authorization": f"Bearer {self.api_key}"})
        try:
            text = str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"{self.name} returned an invalid completion") from exc
        return ModelResult(text, self.model,
                           latency=time.perf_counter() - started)


class AnthropicProvider(HostedProvider):
    """Anthropic Messages API adapter."""

    def list_models(self) -> list[str]:
        request = urllib.request.Request(
            self.endpoint + "/models",
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        return sorted(str(x.get("id")) for x in data.get("data", [])
                      if isinstance(x, dict) and x.get("id"))

    def generate(self, prompt: str, *, context: str = "", task: str = "",
                 instructions: str = "", max_output_tokens: int | None = None,
                 temperature: float | None = None) -> ModelResult:
        started = time.perf_counter()
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": int(max_output_tokens or 1024),
            "messages": [{"role": "user", "content": compose_provider_prompt(
                prompt, context=context, task=task, instructions=instructions)}],
        }
        if temperature is not None:
            body["temperature"] = float(temperature)
        data = self._post(self.endpoint + "/messages", body, {
            "x-api-key": self.api_key, "anthropic-version": "2023-06-01",
        })
        try:
            text = "".join(str(x.get("text", "")) for x in data["content"]
                           if isinstance(x, dict) and x.get("type") == "text")
        except (KeyError, TypeError) as exc:
            raise RuntimeError("Anthropic returned an invalid completion") from exc
        if not text:
            raise RuntimeError("Anthropic returned empty output")
        return ModelResult(text, self.model, latency=time.perf_counter() - started)


class GeminiProvider(HostedProvider):
    """Google Gemini generateContent adapter."""

    def list_models(self) -> list[str]:
        request = urllib.request.Request(
            self.endpoint + "/models?key=" + urllib.parse.quote(self.api_key),
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        return sorted(str(x.get("name", "").removeprefix("models/"))
                      for x in data.get("models", [])
                      if isinstance(x, dict) and x.get("name"))

    def generate(self, prompt: str, *, context: str = "", task: str = "",
                 instructions: str = "", max_output_tokens: int | None = None,
                 temperature: float | None = None) -> ModelResult:
        import urllib.parse
        started = time.perf_counter()
        text_prompt = compose_provider_prompt(
            prompt, context=context, task=task, instructions=instructions)
        generation: dict[str, Any] = {}
        if max_output_tokens is not None:
            generation["maxOutputTokens"] = int(max_output_tokens)
        if temperature is not None:
            generation["temperature"] = float(temperature)
        body: dict[str, Any] = {"contents": [{"parts": [{"text": text_prompt}]}]}
        if generation:
            body["generationConfig"] = generation
        data = self._post(
            self.endpoint + "/models/" + self.model + ":generateContent?key="
            + urllib.parse.quote(self.api_key),
            body, {},
        )
        try:
            text = str(data["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Gemini returned an invalid completion") from exc
        return ModelResult(text, self.model, latency=time.perf_counter() - started)


def build_remote_providers(env: dict[str, str] | None = None) -> list[tuple[str, HostedProvider, str]]:
    e = dict(os.environ if env is None else env)
    specs = [
        ("anthropic", AnthropicProvider, "https://api.anthropic.com/v1",
         e.get("ANTHROPIC_API_KEY", ""), e.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")),
        ("gemini", GeminiProvider, "https://generativelanguage.googleapis.com/v1beta",
         e.get("GEMINI_API_KEY", ""), e.get("GEMINI_MODEL", "gemini-2.5-flash")),
        ("openrouter", OpenAICompatibleProvider, "https://openrouter.ai/api/v1",
         e.get("OPENROUTER_API_KEY", ""), e.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")),
        ("groq", OpenAICompatibleProvider, "https://api.groq.com/openai/v1",
         e.get("GROQ_API_KEY", ""), e.get("GROQ_MODEL", "llama-3.3-70b-versatile")),
    ]
    out = []
    for name, cls, endpoint, key, model in specs:
        if key:
            out.append((name, cls(name, model, key, endpoint), model))
    return out
