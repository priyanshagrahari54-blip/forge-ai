"""Stdlib remote provider adapters for common hosted model APIs.

Each adapter serves *every* model id its account exposes: the routed model is
passed per request (``generate(..., model=...)``) and the constructor model is
only the default. Discovery (``list_models``) is inventory evidence, never a
liveness claim: a model becomes routable only after a real inference probe
succeeds (see :mod:`forge.models.runtime_monitor_service`).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from forge.models.provider import ModelResult, compose_provider_prompt

#: Hard cap on one discovery page; every hosted API supports at least this.
_DISCOVERY_PAGE_SIZE = 1000
#: Upper bound on ids kept from one provider inventory (OpenRouter lists
#: several hundred; nothing needs more than this to reason about eligibility).
MAX_DISCOVERED_MODELS = 2000


def _strip_prefix(value: str, prefix: str) -> str:
    """``str.removeprefix`` for the Python 3.8 CI matrix."""
    return value[len(prefix):] if prefix and value.startswith(prefix) else value


def parse_model_list(raw: str | None) -> tuple[str, ...]:
    """Split a comma/whitespace separated env value into unique model ids."""
    if not raw:
        return ()
    seen: list[str] = []
    for token in raw.replace("\n", ",").replace(" ", ",").split(","):
        model = token.strip()
        if model and model not in seen:
            seen.append(model)
    return tuple(seen)


class HostedProvider:
    """Small, dependency-free hosted-provider adapter with bounded HTTP."""

    #: Set by subclasses when the API lists models; used by the monitor to
    #: decide whether a ``list_models`` failure is a transport problem.
    supports_discovery = True

    def __init__(self, name: str, model: str, api_key: str, endpoint: str,
                 *, timeout: float = 120.0) -> None:
        self.name = name
        self.model = model
        self.api_key = api_key
        self.endpoint = endpoint.rstrip("/")
        self.timeout = max(5.0, min(float(timeout), 600.0))

    # -- HTTP -------------------------------------------------------------

    def _post(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            url, json.dumps(body).encode("utf-8"),
            {"Content-Type": "application/json", **headers},
        )
        return self._send(request, timeout=self.timeout)

    def _get(self, url: str, headers: dict[str, str], *, timeout: float = 20.0) -> dict[str, Any]:
        return self._send(urllib.request.Request(url, headers=headers), timeout=timeout)

    def _send(self, request: urllib.request.Request, *, timeout: float) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            # Keep diagnostics actionable without ever exposing response bodies,
            # which may contain provider-specific request details.
            raise RuntimeError(f"{self.name} HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{self.name} network error: {exc.reason}") from exc
        if len(raw) > 4 * 1024 * 1024:
            raise RuntimeError(f"{self.name} response exceeded 4 MiB limit")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError(f"{self.name} returned a non-object response")
        return data

    @staticmethod
    def _ids(items: Any, key: str) -> list[str]:
        found: list[str] = []
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get(key):
                found.append(str(item[key]))
                if len(found) >= MAX_DISCOVERED_MODELS:
                    break
        return sorted(set(found))

    def _target(self, model: str | None) -> str:
        target = (model or self.model or "").strip()
        if not target:
            raise RuntimeError(f"{self.name}: no model id configured for this request")
        return target


class OpenAICompatibleProvider(HostedProvider):
    """OpenAI-compatible chat-completions provider (OpenRouter/Groq)."""

    def list_models(self) -> list[str]:
        data = self._get(self.endpoint + "/models",
                         {"Authorization": f"Bearer {self.api_key}"})
        return self._ids(data.get("data"), "id")

    def generate(self, prompt: str, *, context: str = "", task: str = "",
                 instructions: str = "", max_output_tokens: int | None = None,
                 temperature: float | None = None,
                 model: str | None = None) -> ModelResult:
        target = self._target(model)
        started = time.perf_counter()
        messages: list[dict[str, str]] = []
        if task.strip():
            messages.append({"role": "system", "content": task.strip()})
        messages.append({"role": "user", "content": compose_provider_prompt(
            prompt, context=context, instructions=instructions)})
        body: dict[str, Any] = {"model": target, "messages": messages}
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
        return ModelResult(text, target, latency=time.perf_counter() - started)


class AnthropicProvider(HostedProvider):
    """Anthropic Messages API adapter."""

    _headers_version = "2023-06-01"

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "anthropic-version": self._headers_version}

    def list_models(self) -> list[str]:
        # The Models API is paginated (default page 20); ask for the largest
        # page and follow ``after_id`` cursors up to the inventory cap.
        found: list[str] = []
        after_id = ""
        for _ in range(8):
            query = {"limit": _DISCOVERY_PAGE_SIZE}
            if after_id:
                query["after_id"] = after_id
            data = self._get(self.endpoint + "/models?" + urllib.parse.urlencode(query),
                             self._headers())
            page = self._ids(data.get("data"), "id")
            found.extend(page)
            if not data.get("has_more") or not data.get("last_id") or len(found) >= MAX_DISCOVERED_MODELS:
                break
            after_id = str(data["last_id"])
        return sorted(set(found))

    def generate(self, prompt: str, *, context: str = "", task: str = "",
                 instructions: str = "", max_output_tokens: int | None = None,
                 temperature: float | None = None,
                 model: str | None = None) -> ModelResult:
        target = self._target(model)
        started = time.perf_counter()
        body: dict[str, Any] = {
            "model": target,
            "max_tokens": int(max_output_tokens or 1024),
            "messages": [{"role": "user", "content": compose_provider_prompt(
                prompt, context=context, task=task, instructions=instructions)}],
        }
        if temperature is not None:
            body["temperature"] = float(temperature)
        data = self._post(self.endpoint + "/messages", body, self._headers())
        try:
            text = "".join(str(x.get("text", "")) for x in data["content"]
                           if isinstance(x, dict) and x.get("type") == "text")
        except (KeyError, TypeError) as exc:
            raise RuntimeError("Anthropic returned an invalid completion") from exc
        if not text:
            raise RuntimeError("Anthropic returned empty output")
        return ModelResult(text, target, latency=time.perf_counter() - started)


class GeminiProvider(HostedProvider):
    """Google Gemini generateContent adapter."""

    def list_models(self) -> list[str]:
        # Only models that support ``generateContent`` are usable through this
        # adapter; embedding/AQA-only entries are not text runtimes.
        found: list[str] = []
        page_token = ""
        for _ in range(8):
            query = {"key": self.api_key, "pageSize": _DISCOVERY_PAGE_SIZE}
            if page_token:
                query["pageToken"] = page_token
            data = self._get(self.endpoint + "/models?" + urllib.parse.urlencode(query), {})
            for item in data.get("models", []) if isinstance(data.get("models"), list) else []:
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                methods = item.get("supportedGenerationMethods")
                if isinstance(methods, list) and "generateContent" not in methods:
                    continue
                found.append(_strip_prefix(str(item["name"]), "models/"))
            page_token = str(data.get("nextPageToken") or "")
            if not page_token or len(found) >= MAX_DISCOVERED_MODELS:
                break
        return sorted(set(found))

    def generate(self, prompt: str, *, context: str = "", task: str = "",
                 instructions: str = "", max_output_tokens: int | None = None,
                 temperature: float | None = None,
                 model: str | None = None) -> ModelResult:
        target = self._target(model)
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
            self.endpoint + "/models/" + urllib.parse.quote(target, safe="")
            + ":generateContent?key=" + urllib.parse.quote(self.api_key),
            body, {},
        )
        try:
            text = str(data["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Gemini returned an invalid completion") from exc
        return ModelResult(text, target, latency=time.perf_counter() - started)


@dataclass(frozen=True)
class HostedProviderSpec:
    """One hosted provider the operator enabled through the environment."""

    name: str
    provider: HostedProvider
    #: Model ids the operator configured (``<NAME>_MODELS`` list, falling back
    #: to the single ``<NAME>_MODEL``). Configured means "asked for", not live.
    models: tuple[str, ...]


#: ``(provider, adapter, endpoint, key var, models var, model var, default)``.
#: The default id is an operator-overridable starting point that is still
#: verified by real inference before routing; it is never reported as live
#: on its own.
_HOSTED_SPECS: tuple[tuple[str, type, str, str, str, str, str], ...] = (
    ("anthropic", AnthropicProvider, "https://api.anthropic.com/v1",
     "ANTHROPIC_API_KEY", "ANTHROPIC_MODELS", "ANTHROPIC_MODEL", "claude-sonnet-4-5"),
    ("gemini", GeminiProvider, "https://generativelanguage.googleapis.com/v1beta",
     "GEMINI_API_KEY", "GEMINI_MODELS", "GEMINI_MODEL", "gemini-2.5-flash"),
    ("openrouter", OpenAICompatibleProvider, "https://openrouter.ai/api/v1",
     "OPENROUTER_API_KEY", "OPENROUTER_MODELS", "OPENROUTER_MODEL", "openai/gpt-4o-mini"),
    ("groq", OpenAICompatibleProvider, "https://api.groq.com/openai/v1",
     "GROQ_API_KEY", "GROQ_MODELS", "GROQ_MODEL", "openai/gpt-oss-120b"),
)


def hosted_provider_specs(env: dict[str, str] | None = None) -> list[HostedProviderSpec]:
    """Build one adapter per hosted provider whose key is present in ``env``."""
    e = dict(os.environ if env is None else env)
    specs: list[HostedProviderSpec] = []
    for name, cls, endpoint, key_var, models_var, model_var, default in _HOSTED_SPECS:
        key = e.get(key_var, "")
        if not key:
            continue
        models = parse_model_list(e.get(models_var))
        primary = (e.get(model_var) or "").strip()
        if primary and primary not in models:
            models = (primary,) + models
        if not models:
            models = (default,)
        specs.append(HostedProviderSpec(name, cls(name, models[0], key, endpoint), models))
    return specs


def build_remote_providers(env: dict[str, str] | None = None) -> list[tuple[str, HostedProvider, tuple[str, ...]]]:
    """Compatibility view of :func:`hosted_provider_specs` as tuples."""
    return [(spec.name, spec.provider, spec.models) for spec in hosted_provider_specs(env)]
