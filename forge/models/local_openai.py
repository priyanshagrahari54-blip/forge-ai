"""Provider for a self-hosted, OpenAI-compatible model endpoint.

Works with any local runtime that speaks the OpenAI HTTP shape — llama.cpp's
``llama-server``, vLLM, Ollama's ``/v1`` compatibility layer, LM Studio,
text-generation-inference. Nothing here requires a cloud credential: the
operator runs the model, Forge routes to it.

Honesty rules this provider follows:

* it is only registered when an endpoint *and* a model name are configured;
* :meth:`list_models` performs a real HTTP probe, so
  :class:`~forge.models.runtime_monitor_service.RuntimeMonitorService` can
  reach a conclusive verdict for this exact model id instead of leaving it
  "unverified";
* capabilities are whatever the operator declares (Forge cannot introspect
  them), and they are reported as declared — a model is never promoted to a
  capability it was not configured for;
* failures raise, so routing failover and telemetry record a real error
  rather than an empty success.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from forge.models.provider import ModelResult


class LocalOpenAIProvider:
    """Talk to a locally hosted OpenAI-compatible endpoint."""

    name = "local-openai"

    def __init__(self, model: str, url: str, *, api_key: str = "",
                 timeout: float = 120.0,
                 capabilities: tuple[str, ...] = (),
                 send_constraints: bool = False) -> None:
        if not model:
            raise ValueError("a local model name is required")
        if not url:
            raise ValueError("a local endpoint url is required")
        self.model = model
        self.base_url = _normalize_base(url)
        self.url = self.base_url + "/chat/completions"
        self.api_key = api_key
        self.timeout = max(1.0, float(timeout))
        self.capabilities = tuple(capabilities)
        #: Forge renders its routing/generation constraints as an instruction
        #: block. That block is operator metadata ("required capabilities:
        #: coding", "maximum output tokens: 512", "prefer local provider"): a
        #: *chat* endpoint would receive it as if the user had asked for it, so
        #: by default the applicable limits go to the API's native fields
        #: (``max_tokens``, ``temperature``) and the rest stays in the result
        #: metadata. Measured on a small local instruct model, injecting the
        #: block dominated the prompt and the model echoed it instead of
        #: answering. Operators whose endpoint expects the block can opt in.
        self.send_constraints = bool(send_constraints)

    # -- transport ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _post(self, path: str, body: dict[str, Any],
              timeout: float | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path, json.dumps(body).encode(),
            self._headers())
        try:
            with urllib.request.urlopen(
                    request, timeout=timeout or self.timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode()[:300]
            except Exception:                                 # noqa: BLE001
                pass
            raise RuntimeError(
                f"local model {self.model!r} at {self.base_url} returned "
                f"HTTP {exc.code}: {detail}") from exc
        except Exception as exc:                              # noqa: BLE001
            raise RuntimeError(
                f"local model {self.model!r} is unreachable at "
                f"{self.base_url}: {exc}") from exc

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        """Accept the OpenAI chat shape and llama.cpp's native shape."""
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0] or {}
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict) and "content" in message:
                    return str(message.get("content") or "")
                if "text" in first:
                    return str(first.get("text") or "")
        if "content" in data:
            return str(data.get("content") or "")
        if "response" in data:                                # ollama native
            return str(data.get("response") or "")
        return ""

    # -- provider contract -------------------------------------------------

    def generate(self, prompt: str, *, context: str = "", task: str = "",
                 instructions: str = "", max_output_tokens: int | None = None,
                 temperature: float | None = None) -> ModelResult:
        from forge.models.provider import compose_provider_prompt

        started = time.perf_counter()
        messages: list[dict[str, str]] = []
        if task and task.strip():
            messages.append({"role": "system", "content": task.strip()})
        messages.append({
            "role": "user",
            "content": compose_provider_prompt(
                prompt, context=context,
                instructions=instructions if self.send_constraints else ""),
        })
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        if max_output_tokens is not None:
            body["max_tokens"] = int(max_output_tokens)
        if temperature is not None:
            body["temperature"] = float(temperature)
        data = self._post("/chat/completions", body)
        text = self._extract_text(data)
        usage = data.get("usage") or {}
        metadata: dict[str, Any] = {}
        if not self.send_constraints and instructions and instructions.strip():
            #: Not silently dropped: routing constraints are recorded on the
            #: result (and merged into the fabric response metadata), they are
            #: just not addressed to the model as if they were user intent.
            metadata["routing_constraints"] = instructions.strip()
        return ModelResult(
            text, self.model,
            latency=time.perf_counter() - started,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            metadata=metadata,
        )

    def list_models(self) -> list[str]:
        """Real probe: ask the endpoint which models it is serving."""
        request = urllib.request.Request(self.base_url + "/models",
                                         headers=self._headers())
        try:
            with urllib.request.urlopen(
                    request, timeout=min(20.0, self.timeout)) as response:
                data = json.loads(response.read().decode())
        except Exception as exc:                              # noqa: BLE001
            raise RuntimeError(
                f"local endpoint {self.base_url} did not answer /models: "
                f"{exc}") from exc
        items = data.get("data") if isinstance(data, dict) else None
        if items is None and isinstance(data, dict):
            items = data.get("models")
        names: set[str] = set()
        for item in items or []:
            if isinstance(item, dict):
                value = item.get("id") or item.get("name") or item.get("model")
            else:
                value = item
            if not value:
                continue
            text = str(value)
            names.add(text)
            # llama.cpp's llama-server reports the model *file path* as its
            # id, so the configured name (the file name) must also match;
            # otherwise a correctly configured local model would be reported
            # as "not_found" by the runtime probe.
            base = text.replace("\\", "/").rsplit("/", 1)[-1]
            if base:
                names.add(base)
        return sorted(names)

    def health(self) -> dict[str, Any]:
        models = self.list_models()
        return {"available": self.model in models, "models": models,
                "endpoint": self.base_url}


def _normalize_base(url: str) -> str:
    """Return ``<scheme>://<host>[:port]/v1`` for any reasonable input."""
    value = str(url).strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    if not value.endswith("/v1") and "/v1/" not in value:
        value += "/v1"
    return value.rstrip("/")


__all__ = ["LocalOpenAIProvider"]
