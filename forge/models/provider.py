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


def compose_provider_prompt(
    prompt: str,
    *,
    context: str = "",
    task: str = "",
    instructions: str = "",
) -> str:
    """Compose the effective model input from structured request parts.

    Providers whose API only accepts a single prompt use this to preserve the
    semantic separation of task / instructions / repository context /
    constraints as labeled sections, so no structured request information is
    silently dropped at the provider boundary.
    """
    sections: list[tuple[str, str]] = []
    if task and task.strip():
        sections.append(("TASK", task.strip()))
    if prompt and prompt.strip():
        sections.append(("INSTRUCTIONS", prompt.strip()))
    if context and context.strip():
        sections.append(("REPOSITORY CONTEXT", context.strip()))
    if instructions and instructions.strip():
        sections.append(("CONSTRAINTS", instructions.strip()))
    if not sections:
        return ""
    return "\n\n".join(f"{label}\n{body}" for label, body in sections)


class ModelProvider(Protocol):
    """Minimal provider contract: a name and a synchronous ``generate``.

    Providers accept ``context`` (repository context), ``task``, and optionally
    ``instructions`` (routing/generation constraints), ``max_output_tokens``,
    and ``temperature``. Providers that only understand a subset of these
    keyword arguments are invoked with only the subset they declare; the Model
    Fabric introspects the callable and never passes unsupported keywords.
    """

    name: str

    def generate(
        self,
        prompt: str,
        *,
        context: str = "",
        task: str = "",
        instructions: str = "",
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ModelResult:
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
    """Safe offline fallback; it refuses arbitrary synthesis instead of faking it.

    The full request (task, context, instructions) is accepted for interface
    parity with real providers but is intentionally not turned into code: the
    local provider is a deterministic no-op that tells the caller to configure
    a real model.
    """

    name = "local"

    def generate(self, prompt: str, *, context: str = "", task: str = "",
                 instructions: str = "", max_output_tokens: int | None = None,
                 temperature: float | None = None) -> ModelResult:
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

    def __init__(self, model: str = "llama3.2", url: str | None = None, timeout: float = 120.0):
        self.model = model
        self.timeout = timeout
        base = (
            url
            or os.getenv("OLLAMA_BASE_URL")
            or os.getenv("OLLAMA_URL")
            or "http://127.0.0.1:11434"
        )
        # The base URL may be a bare host (the conventional OLLAMA_BASE_URL
        # form). Normalize so the provider always talks to /api/generate and
        # never POSTs a completion to the server root.
        normalized = base.rstrip("/")
        self.url = (
            normalized
            if normalized.endswith("/api/generate")
            else normalized + "/api/generate"
        )

    def _body(self, prompt: str, *, stream: bool, context: str = "", task: str = "",
              instructions: str = "", max_output_tokens: int | None = None,
              temperature: float | None = None) -> dict:
        """Build the Ollama request body with the full structured request.

        The task is placed in the native ``system`` slot; the instructions,
        repository context, and constraints are composed into the ``prompt``.
        ``options`` carries ``num_predict`` and ``temperature`` natively.
        """
        body: dict[str, Any] = {"model": self.model, "stream": stream}
        if task and task.strip():
            body["system"] = task.strip()
        body["prompt"] = compose_provider_prompt(
            prompt, context=context, instructions=instructions
        )
        options: dict[str, Any] = {}
        if max_output_tokens is not None:
            options["num_predict"] = int(max_output_tokens)
        if temperature is not None:
            options["temperature"] = float(temperature)
        if options:
            body["options"] = options
        return body

    def generate(self, prompt: str, *, context: str = "", task: str = "",
                 instructions: str = "", max_output_tokens: int | None = None,
                 temperature: float | None = None) -> ModelResult:
        started = time.perf_counter()
        payload = json.dumps(self._body(
            prompt, stream=False, context=context, task=task,
            instructions=instructions, max_output_tokens=max_output_tokens,
            temperature=temperature,
        )).encode()
        request = urllib.request.Request(self.url, payload, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode())
        except Exception as exc:
            raise RuntimeError(f"Ollama model {self.model!r} unavailable at {self.url}: {exc}") from exc
        return ModelResult(str(data.get("response", "")), self.model,
                           latency=time.perf_counter() - started)

    def stream(self, prompt: str, *, context: str = "", task: str = "",
               instructions: str = "", max_output_tokens: int | None = None,
               temperature: float | None = None):
        """Yield response deltas from Ollama's streaming endpoint.

        The contract mirrors ``generate`` but yields text fragments. A provider
        failure raises ``RuntimeError`` like ``generate`` so callers share one
        error path.
        """
        payload = json.dumps(self._body(
            prompt, stream=True, context=context, task=task,
            instructions=instructions, max_output_tokens=max_output_tokens,
            temperature=temperature,
        )).encode()
        request = urllib.request.Request(self.url, payload, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    chunk = data.get("response", "")
                    if chunk:
                        yield chunk
                    if data.get("done"):
                        break
        except Exception as exc:
            raise RuntimeError(f"Ollama model {self.model!r} stream failed at {self.url}: {exc}") from exc

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

    def generate(self, prompt: str, *, context: str = "", task: str = "",
                 instructions: str = "", max_output_tokens: int | None = None,
                 temperature: float | None = None) -> ModelResult:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        started = time.perf_counter()
        # Native chat structure: the task is the system message, the composed
        # instructions/context/constraints are the user message.
        messages: list[dict[str, str]] = []
        if task and task.strip():
            messages.append({"role": "system", "content": task.strip()})
        user_content = compose_provider_prompt(
            prompt, context=context, instructions=instructions
        )
        messages.append({"role": "user", "content": user_content})
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        if max_output_tokens is not None:
            body["max_tokens"] = int(max_output_tokens)
        if temperature is not None:
            body["temperature"] = float(temperature)
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
