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
from typing import Any, Sequence

from forge.models.endpoints import LocalEndpoint
from forge.models.errors import ProviderExhaustedError
from forge.models.provider import ModelResult
from forge.models.quota import (
    DEFAULT_COOLDOWN_SECONDS,
    ExhaustionTracker,
    QuotaSignal,
    classify_exhaustion,
)


def _retry_after_header(exc: urllib.error.HTTPError) -> float | None:
    """The delay an endpoint stated, in seconds, when it stated one."""
    for header in ("Retry-After", "retry-after", "X-RateLimit-Reset"):
        raw = None
        try:
            raw = exc.headers.get(header) if exc.headers else None
        except Exception:                                     # noqa: BLE001
            raw = None
        if not raw:
            continue
        try:
            value = float(str(raw).strip())
        except ValueError:
            continue
        # X-RateLimit-Reset is commonly an epoch timestamp, not a delay.
        if value > 1_000_000_000:
            import time as _time
            value = max(0.0, value - _time.time())
        return max(0.0, value)
    return None


class LocalOpenAIProvider:
    """Talk to a locally hosted OpenAI-compatible endpoint."""

    name = "local-openai"

    def __init__(self, model: str, url: str = "", *, api_key: str = "",
                 timeout: float = 120.0,
                 capabilities: tuple[str, ...] = (),
                 send_constraints: bool = False,
                 endpoints: Sequence[LocalEndpoint] | None = None,
                 cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
                 exhaustion: ExhaustionTracker | None = None) -> None:
        if not model:
            raise ValueError("a local model name is required")
        #: One provider can front several of the operator's servers for the
        #: same model. A request that finds an endpoint spent (HTTP 429, quota
        #: or rate limit) is retried on the next one *within this call*, and
        #: only if every endpoint is spent does the provider raise
        #: ProviderExhaustedError for the fabric to fail over to another model.
        pool: list[LocalEndpoint] = []
        if str(url).strip():
            pool.append(LocalEndpoint(url=url, model=model, api_key=api_key,
                                      timeout=timeout,
                                      capabilities=tuple(capabilities)))
        if endpoints:
            pool = [LocalEndpoint(
                url=entry.url, model=entry.model or model,
                capabilities=tuple(entry.capabilities or capabilities),
                context_window=entry.context_window,
                timeout=entry.timeout or timeout,
                api_key=entry.api_key or api_key,
                tier=entry.tier, label=entry.label,
                metadata=dict(entry.metadata),
            ) for entry in endpoints]
        if not pool or not pool[0].url:
            raise ValueError("a local endpoint url is required")
        self.model = model
        self.endpoints = tuple(pool)
        self.base_url = _normalize_base(pool[0].url)
        self.url = self.base_url + "/chat/completions"
        self.api_key = pool[0].api_key
        self.timeout = max(1.0, float(pool[0].timeout or timeout))
        self.capabilities = tuple(capabilities)
        #: Per-endpoint cooldowns, shared with the fabric when supplied so a
        #: spent endpoint is skipped by every caller, not just this provider.
        self.exhaustion = exhaustion if exhaustion is not None else \
            ExhaustionTracker(cooldown_seconds=cooldown_seconds)
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

    @staticmethod
    def _headers_for(endpoint: LocalEndpoint) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if endpoint.api_key:
            headers["Authorization"] = f"Bearer {endpoint.api_key}"
        return headers

    def _headers(self) -> dict[str, str]:
        return self._headers_for(self.endpoints[0])

    def _post(self, path: str, body: dict[str, Any],
              timeout: float | None = None,
              endpoint: LocalEndpoint | None = None) -> dict[str, Any]:
        target = endpoint or self.endpoints[0]
        base_url = _normalize_base(target.url)
        request = urllib.request.Request(
            base_url + path, json.dumps(body).encode(),
            self._headers_for(target))
        try:
            with urllib.request.urlopen(
                    request, timeout=timeout or target.timeout
                    or self.timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode()[:300]
            except Exception:                                 # noqa: BLE001
                pass
            message = (f"local model {self.model!r} at {base_url} returned "
                       f"HTTP {exc.code}: {detail}")
            retry_after = _retry_after_header(exc)
            #: Classify the endpoint's own words, never a pre-typed wrapper:
            #: wrapping first would make every HTTP error look exhausted.
            signal = classify_exhaustion(message)
            if exc.code in (402, 429):
                signal = QuotaSignal(True, retry_after=retry_after,
                                     reason=message, status=exc.code)
            if signal.exhausted:
                #: The endpoint is spent, not broken: say so, with the delay
                #: it asked for, so routing can move to another server.
                raise ProviderExhaustedError(
                    message, provider=self.name, model=self.model,
                    retry_after=retry_after, status=exc.code) from exc
            raise RuntimeError(message) from exc
        except ProviderExhaustedError:
            raise
        except Exception as exc:                              # noqa: BLE001
            raise RuntimeError(
                f"local model {self.model!r} is unreachable at "
                f"{base_url}: {exc}") from exc

    def _post_across_pool(self, path: str, body: dict[str, Any],
                          timeout: float | None = None
                          ) -> tuple[dict[str, Any], LocalEndpoint, list[str]]:
        """POST to the pool, skipping endpoints whose quota is spent.

        Rotation is immediate and within the same request: an exhausted
        endpoint costs this call nothing but the error that revealed it. When
        every endpoint is spent the provider raises ``ProviderExhaustedError``
        — with the shortest stated retry delay — so the fabric fails over to a
        different model instead of hammering a wall.
        """
        rotated: list[str] = []
        last_signal: QuotaSignal | None = None
        for endpoint in self.endpoints:
            if self.exhaustion.is_exhausted(self.name, endpoint.url):
                rotated.append(endpoint.display)
                continue
            try:
                data = self._post(path, body, timeout=timeout, endpoint=endpoint)
            except ProviderExhaustedError as exc:
                signal = QuotaSignal(True, retry_after=exc.retry_after,
                                     reason=str(exc), status=exc.status)
                self.exhaustion.record(self.name, endpoint.url, signal)
                last_signal = signal
                rotated.append(endpoint.display)
                continue
            self.exhaustion.clear(self.name, endpoint.url)
            return data, endpoint, rotated
        retry_after = last_signal.retry_after if last_signal else None
        detail = last_signal.reason if last_signal else ""
        raise ProviderExhaustedError(
            f"every configured endpoint for model {self.model!r} is "
            f"exhausted or spent ({'; '.join(rotated) or 'none reachable'})"
            + (f": {detail}" if detail else ""),
            provider=self.name, model=self.model, retry_after=retry_after,
            status=last_signal.status if last_signal else None)

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
        data, target, rotated = self._post_across_pool("/chat/completions", body)
        text = self._extract_text(data)
        usage = data.get("usage") or {}
        metadata: dict[str, Any] = {}
        metadata["endpoint_used"] = target.url
        if target.label:
            metadata["endpoint_label"] = target.label
        if rotated:
            #: Rotation across the operator's servers is provenance: a caller
            #: can see the first endpoint was spent and which one answered.
            metadata["endpoints_skipped_exhausted"] = rotated
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
        """Real probe: ask each endpoint which models it is serving.

        One answering endpoint is enough — a probe is evidence that the model
        is being served, and an exhausted endpoint still lists it. Only when
        none answers is the provider reported unreachable.
        """
        last_error: Exception | None = None
        for endpoint in self.endpoints:
            try:
                return self._probe_models(endpoint)
            except Exception as exc:                          # noqa: BLE001
                last_error = exc
                continue
        raise RuntimeError(
            f"no configured endpoint answered a model-list probe: {last_error}")

    def _probe_models(self, endpoint: LocalEndpoint) -> list[str]:
        base_url = _normalize_base(endpoint.url)
        request = urllib.request.Request(base_url + "/models",
                                         headers=self._headers_for(endpoint))
        try:
            with urllib.request.urlopen(
                    request, timeout=min(20.0, endpoint.timeout
                                         or self.timeout)) as response:
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
