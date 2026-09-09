"""Real OpenAI/ChatGPT connector for AI-to-AI collaboration (A44).

This connector makes real HTTP calls to the OpenAI API. It is
permission-gated exactly like the simulated connector (Resource.MODEL /
call), and every response is still marked untrusted. API keys come from
the ``OPENAI_API_KEY`` environment variable; if unset, the connector
is unavailable and ``available()`` returns False.

Design invariants (same as A44 simulated connector):

* Every response carries ``source="external_ai"``, ``untrusted=True``,
  the real provider/model label, and ``simulation=False``.
* No real network happens without a configured API key.
* Responses are bounded to ``MAX_CONTENT`` characters.
* The connector never executes anything, writes nothing, and grants
  nothing — external answers remain untrusted context only.
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

MAX_CONTENT = 6000
DEFAULT_MODEL = "gpt-4o-mini"
REQUEST_TIMEOUT = 30.0


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
                 base_url: str = "") -> None:
        self.model = model or os.environ.get(
            "FORGE_OPENAI_MODEL", DEFAULT_MODEL)
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._base_url = base_url or os.environ.get(
            "OPENAI_BASE_URL", "https://api.openai.com/v1")

    def available(self) -> bool:
        return bool(self._api_key)

    def ask(self, prompt: str, *, context: str = "") -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be non-empty")
        prompt = prompt.strip()[:MAX_PROMPT]
        context = (context or "")[:MAX_CONTEXT]
        if not self._api_key:
            return ExternalAIResponse(
                connector=self.name, model=self.model,
                content=("[openai] No OPENAI_API_KEY configured; "
                         "the connector is unavailable."),
                untrusted=True, source="external_ai",
                simulation=False,
            ).to_dict()
        started = time.time()
        content = self._call_api(prompt, context)
        latency_ms = (time.time() - started) * 1000.0
        response = ExternalAIResponse(
            connector=self.name, model=self.model,
            content=content[:MAX_CONTENT],
            untrusted=True, source="external_ai",
            simulation=False, latency_ms=latency_ms,
        )
        return response.to_dict()

    def _call_api(self, prompt: str, context: str) -> str:
        """Make a real OpenAI ChatCompletion call."""
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
                return message.get("content", "") or ""
            return "[openai] Empty response from API."
        except urllib.error.HTTPError as exc:
            return (f"[openai] API returned HTTP {exc.code}: "
                    f"{exc.reason}")
        except urllib.error.URLError as exc:
            return f"[openai] Connection error: {exc.reason}"
        except Exception as exc:
            return f"[openai] Request failed: {exc}"
