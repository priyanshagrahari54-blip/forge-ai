"""Real OpenAI/ChatGPT connector for AI-to-AI collaboration (A44).

This connector makes real HTTP calls to the OpenAI API. It is
permission-gated exactly like the simulated connector (Resource.MODEL /
call), and every response is still marked untrusted. API keys come from
the ``OPENAI_API_KEY`` environment variable; if unset, the connector
is unavailable and ``available()`` returns False.

Design invariants:

* Every response carries ``source="external_ai"``, ``untrusted=True``,
  the real provider/model label, and ``simulation=False``.
* A failed provider request is NEVER represented as a successful model
  response: each result carries an explicit ``state`` — SUCCESS /
  PROVIDER_ERROR / TIMEOUT / RATE_LIMITED / AUTH_ERROR /
  POLICY_DENIED / UNAVAILABLE — and ``ok == (state == SUCCESS)``.
  On failure ``content`` is empty and ``error`` explains the state.
* ``simulation=false`` never means "request succeeded".
* No real network happens without a configured API key. Responses are
  bounded, retries are bounded, structured errors are mapped from the
  HTTP status, and no secrets are logged or echoed back.
"""
from __future__ import annotations

import os
import time
from typing import Any

from forge.collaboration.connectors import (
    ExternalAIResponse,
    MAX_CONTEXT,
    MAX_PROMPT,
)
from forge.security.provider_states import (
    ProviderState,
    classify_http_status,
    state_dict,
)

MAX_CONTENT = 6000
DEFAULT_MODEL = "gpt-4o-mini"
REQUEST_TIMEOUT = 30.0
MAX_RETRIES = 1
RETRY_BACKOFF = 0.5


class OpenAIConnector:
    """Real OpenAI ChatCompletion connector.

    Requires ``OPENAI_API_KEY``. Falls back to unavailable when the key
    is absent, so ``build_connector`` can still resolve the name and
    callers get an honest capability report.
    """

    name = "openai"
    model = DEFAULT_MODEL

    def __init__(self, *, model: str = "",
                 api_key: str = "",
                 base_url: str = "",
                 retry_backoff: float = RETRY_BACKOFF) -> None:
        self.model = model or os.environ.get(
            "FORGE_OPENAI_MODEL", DEFAULT_MODEL)
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._base_url = base_url or os.environ.get(
            "OPENAI_BASE_URL", "https://api.openai.com/v1")
        self._retry_backoff = retry_backoff

    def available(self) -> bool:
        return bool(self._api_key)

    def health(self) -> dict[str, Any]:
        """Structured standing health for the cockpit/router."""
        if not self.available():
            return {"provider": self.name, "status": "UNAVAILABLE",
                    "configured": False,
                    "note": "OPENAI_API_KEY is not configured."}
        return {"provider": self.name, "status": "AVAILABLE",
                "configured": True,
                "model": self.model,
                "note": "Configuration present; call outcomes carry the "
                        "explicit state of every request."}

    def ask(self, prompt: str, *, context: str = "") -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be non-empty")
        prompt = prompt.strip()[:MAX_PROMPT]
        context = (context or "")[:MAX_CONTEXT]
        started = time.time()
        if not self.available():
            return {
                **ExternalAIResponse(
                    connector=self.name, model=self.model,
                    content="", untrusted=True, source="external_ai",
                    simulation=False,
                ).to_dict(),
                **state_dict(ProviderState.UNAVAILABLE, error=(
                    "[openai] No OPENAI_API_KEY configured; the "
                    "connector is unavailable.")),
                "latency_ms": (time.time() - started) * 1000.0,
            }

        state, content = self._call_api(prompt, context)
        latency_ms = (time.time() - started) * 1000.0
        if state is not ProviderState.SUCCESS:
            return {
                **ExternalAIResponse(
                    connector=self.name, model=self.model,
                    content="", untrusted=True, source="external_ai",
                    simulation=False,
                ).to_dict(),
                **state_dict(state, error=content),
                "latency_ms": latency_ms,
            }
        return {
            **ExternalAIResponse(
                connector=self.name, model=self.model,
                content=content[:MAX_CONTENT],
                untrusted=True, source="external_ai",
                simulation=False, latency_ms=latency_ms,
            ).to_dict(),
            **state_dict(ProviderState.SUCCESS),
        }

    def _call_api(self, prompt: str, context: str
                  ) -> tuple[ProviderState, str]:
        """One bounded, retried ChatCompletion attempt.

        Returns ``(state, content_or_error)``; a non-SUCCESS state
        never carries model output in the content slot of the caller.
        """
        import json
        import urllib.request
        import urllib.error

        messages: list[dict[str, str]] = []
        if context:
            messages.append({"role": "system",
                             "content": context[:MAX_CONTEXT]})
        messages.append({"role": "user", "content": prompt})

        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "max_tokens": 1500,
            "temperature": 0.7,
        }).encode("utf-8")

        url = f"{self._base_url.rstrip('/')}/chat/completions"
        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                })
            try:
                with urllib.request.urlopen(
                        req, timeout=REQUEST_TIMEOUT) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                choices = data.get("choices") or []
                if choices:
                    message = choices[0].get("message", {})
                    content = message.get("content", "") or ""
                    if not content.strip():
                        return (ProviderState.PROVIDER_ERROR,
                                "[openai] Empty response from API.")
                    return ProviderState.SUCCESS, content
                return (ProviderState.PROVIDER_ERROR,
                        "[openai] API response contained no choices.")
            except urllib.error.HTTPError as exc:
                state = classify_http_status(exc.code)
                detail = f"[openai] API returned HTTP {exc.code}: {exc.reason}"
                if state.retryable and attempt <= MAX_RETRIES:
                    if self._retry_backoff:
                        time.sleep(self._retry_backoff * attempt)
                    continue
                return state, detail
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", exc)
                if isinstance(reason, TimeoutError) or \
                        "timed out" in str(reason):
                    state = ProviderState.TIMEOUT
                    detail = (f"[openai] Request timed out after "
                              f"{REQUEST_TIMEOUT}s")
                else:
                    state = ProviderState.UNAVAILABLE
                    detail = f"[openai] Connection error: {reason}"
                if state.retryable and attempt <= MAX_RETRIES:
                    if self._retry_backoff:
                        time.sleep(self._retry_backoff * attempt)
                    continue
                return state, detail
            except TimeoutError:
                state = ProviderState.TIMEOUT
                if state.retryable and attempt <= MAX_RETRIES:
                    if self._retry_backoff:
                        time.sleep(self._retry_backoff * attempt)
                    continue
                return state, f"[openai] Request timed out after {REQUEST_TIMEOUT}s"  # noqa: E501
            except Exception as exc:  # noqa: BLE001 — transport catch-all
                return (ProviderState.PROVIDER_ERROR,
                        f"[openai] Request failed: {exc}")
