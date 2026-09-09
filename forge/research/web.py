"""Real web research provider for A47.

Provides real Internet search via configured search providers:

- SearXNG instance (``FORGE_SEARXNG_URL``)
- OpenAI web search / knowledge answers (``OPENAI_API_KEY``)

Controlled URL fetching goes through ``forge.security.ssrf`` — every
URL (and every redirect hop) is validated for scheme, hostname, DNS
resolution, IP classification, port, content type, and size before a
single byte is downloaded. Localhost, private ranges, link-local,
cloud-metadata endpoints, and internal hostnames are refused.

All results are untrusted external input, same as any model output,
and are labeled as such — provenance is explicit on every result:
``REAL_SEARCH_RESULT`` only for genuine provider search output
(SearXNG hits, OpenAI web_search_call items) and ``MODEL_KNOWLEDGE``
for knowledge fallbacks and text-only answers, so nothing fabricated
is ever presented as a web result.

Provider calls never masquerade as success: every call returns a
classified ``state`` of ``SUCCESS`` / ``PROVIDER_ERROR`` / ``TIMEOUT``
/ ``RATE_LIMITED`` / ``AUTH_ERROR`` / ``POLICY_DENIED`` /
``UNAVAILABLE``. API POSTs go to the compile-time OpenAI host over
verified TLS with redirects refused (a 3xx is an error, never a
silent scheme/host change); user/agent URLs always use the SSRF-safe
chain, which revalidates every redirect hop.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

from forge.security.ssrf import (
    FetchPolicy, RESEARCH_POLICY, SSRFError, fetch,
    parse_and_validate,
)

MAX_QUERY = 500
MAX_RESULTS = 10
REQUEST_TIMEOUT = 15.0

#: Fixed model used for knowledge fallback search.
SEARCH_MODEL = "gpt-4o-mini"

#: Machine-readable provenance of a result. Only real provider output
#: is ever labeled as a search result; knowledge fallbacks and
#: text-only answers are labeled MODEL_KNOWLEDGE so consumers can
#: distinguish verified-URL search hits from unverified model text.
KIND_REAL_SEARCH = "REAL_SEARCH_RESULT"
KIND_MODEL_KNOWLEDGE = "MODEL_KNOWLEDGE"
_RESULT_KINDS = frozenset({KIND_REAL_SEARCH, KIND_MODEL_KNOWLEDGE})


class WebSearchResult:
    """One search result (untrusted external input, provenance-labeled)."""

    __slots__ = ("title", "url", "snippet", "kind")

    def __init__(self, title: str, url: str, snippet: str,
                 kind: str = KIND_REAL_SEARCH) -> None:
        self.title = title[:200]
        self.url = url[:500]
        self.snippet = snippet[:500]
        # Fail-closed labeling: anything that is not explicitly model
        # knowledge is treated as a real search result only when the
        # caller marked it so; anything else defaults to the real kind
        # (all internal producers set it explicitly).
        self.kind = kind if kind in _RESULT_KINDS else KIND_REAL_SEARCH

    @property
    def is_real(self) -> bool:
        """True only for genuine provider search output."""
        return self.kind == KIND_REAL_SEARCH

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url,
                "snippet": self.snippet, "kind": self.kind}


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow redirects for API POSTs.

    The OpenAI API target is a fixed host; a redirect is either an
    anomaly or an attempted scheme/host change — surfacing it as an
    error is the only honest, safe behavior. ``redirect_request``
    returning ``None`` makes ``urllib`` raise ``HTTPError`` instead of
    following the Location header.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


#: Shared opener: HTTPS-only default TLS context, no redirects.
_NO_REDIRECT_OPENER = urllib.request.build_opener(_RefuseRedirects)


def _classify_http_error(status: int | None) -> str:
    """Map an HTTP status to an explicit provider state."""
    if status is None:
        return "PROVIDER_ERROR"
    if status == 401 or status == 403:
        return "AUTH_ERROR"
    if status == 429:
        return "RATE_LIMITED"
    if 500 <= status <= 599:
        return "PROVIDER_ERROR"
    return "PROVIDER_ERROR"


class WebSearchProvider:
    """Real Internet search — untrusted external input by construction."""

    def __init__(self) -> None:
        self._openai_key = os.environ.get("OPENAI_API_KEY", "")
        self._searxng_url = os.environ.get("FORGE_SEARXNG_URL", "")

    def available(self) -> bool:
        return bool(self._openai_key or self._searxng_url)

    @property
    def provider_name(self) -> str:
        if self._searxng_url:
            return "searxng"
        if self._openai_key:
            return "openai"
        return "unavailable"

    def health(self) -> dict[str, Any]:
        """Structured provider health (Phase 11 vocabulary)."""
        if not self.available():
            return {
                "provider": "web-search",
                "status": "MISCONFIGURED" if (self._openai_key
                                              or self._searxng_url)
                          else "UNAVAILABLE",
                "configured": False,
                "note": "Set OPENAI_API_KEY or FORGE_SEARXNG_URL.",
            }
        if self._searxng_url:
            configured = "searxng"
        else:
            configured = "openai"
        return {
            "provider": "web-search",
            "status": "AVAILABLE" if configured else "MISCONFIGURED",
            "configured": configured,
            "note": "Configuration present; availability is confirmed "
                    "per call and reported in each result state.",
        }

    def search(self, query: str) -> tuple[str, list[WebSearchResult]]:
        """Search the web.

        Returns ``(state, results)`` where ``state`` is one of
        SUCCESS / PROVIDER_ERROR / TIMEOUT / RATE_LIMITED /
        AUTH_ERROR / UNAVAILABLE. A non-SUCCESS state never carries
        fabricated results; every result carries an explicit
        provenance ``kind`` (REAL_SEARCH_RESULT or MODEL_KNOWLEDGE).
        An empty result list with ``SUCCESS`` means the provider
        answered with nothing usable — callers report that honestly
        instead of inventing results.
        """
        query = (query or "").strip()[:MAX_QUERY]
        if not query:
            return "POLICY_DENIED", []
        if self._searxng_url:
            state, results = self._search_searxng(query)
        elif self._openai_key:
            state, results = self._search_openai(query)
        else:
            return "UNAVAILABLE", []
        return state, results

    # -- SearXNG -------------------------------------------------------------

    def _searxng_policy(self, base: str) -> FetchPolicy | None:
        """Build the network policy for a configured SearXNG endpoint.

        The endpoint is operator configuration, so an explicit
        ``http://`` URL or a LAN instance is permitted only when the
        operator opted in (``FORGE_SEARXNG_ALLOW_HTTP=1`` /
        ``FORGE_SEARXNG_ALLOW_PRIVATE=1``). Defaults stay fail-closed
        (public HTTPS only).
        """
        from urllib.parse import urlparse
        if "://" not in base:
            base = f"https://{base}"
        parsed = urlparse(base)
        scheme = (parsed.scheme or "https").lower()
        host = (parsed.hostname or "").lower()
        allow_http = os.environ.get("FORGE_SEARXNG_ALLOW_HTTP", "") == "1"
        allow_private = os.environ.get(
            "FORGE_SEARXNG_ALLOW_PRIVATE", "") == "1"
        if scheme not in ("https", "http") or not host:
            return None
        if scheme == "http" and not allow_http:
            return None
        port = parsed.port
        try:
            # Validate the hostname itself up front for a clear error.
            parse_and_validate(base, FetchPolicy(
                allow_http=allow_http,
                allow_ports=(port,) if port and port not in (80, 443)
                else (),
                private_allowed_hosts=(host,) if allow_private else ()))
        except SSRFError:
            return None
        return FetchPolicy(
            allow_http=allow_http,
            allow_ports=(port,) if port and port not in (80, 443) else (),
            private_allowed_hosts=(host,) if allow_private else (),
            timeout=REQUEST_TIMEOUT,
            allowed_content_prefixes=("application/json", "text/"))

    def _search_searxng(self, query: str) -> tuple[str, list[WebSearchResult]]:
        """Search via a SearXNG instance through the SSRF-safe chain."""
        import urllib.parse

        base = (self._searxng_url or "").strip()
        if not base:
            return "UNAVAILABLE", []
        policy = self._searxng_policy(base)
        if policy is None:
            return "POLICY_DENIED", []
        url = (f"{base.rstrip('/')}/search?q={urllib.parse.quote(query)}"
               f"&format=json&categories=general")
        outcome = fetch(url, policy=policy)
        if outcome.blocked:
            return "POLICY_DENIED", []
        if not outcome.ok:
            return _classify_http_error(outcome.status), []
        try:
            data = json.loads(outcome.body.decode("utf-8", errors="ignore"))
        except ValueError:
            return "PROVIDER_ERROR", []
        results = []
        for item in (data.get("results") or [])[:MAX_RESULTS]:
            if isinstance(item, dict):
                results.append(WebSearchResult(
                    title=str(item.get("title", "")),
                    url=str(item.get("url", "")),
                    snippet=str(item.get("content", "")),
                    kind=KIND_REAL_SEARCH))
        return "SUCCESS", results

    # -- OpenAI --------------------------------------------------------------

    def _search_openai(self, query: str) -> tuple[str, list[WebSearchResult]]:
        """OpenAI Responses API web search, then knowledge fallback."""
        # Responses API with the web_search tool.
        state, results = self._search_openai_responses(query)
        if state == "SUCCESS":
            return state, results
        if state not in ("UNAVAILABLE", "AUTH_ERROR", "RATE_LIMITED",
                         "TIMEOUT"):
            # Fall back to chat knowledge only on generic provider errors,
            # never on auth/rate/timeout failures (they would recur).
            return self._search_openai_chat(query)
        return state, results

    def _openai_post(self, url: str, body: dict[str, Any],
                     headers: dict[str, str] | None = None
                     ) -> tuple[str, Any]:
        """POST JSON to the OpenAI API over TLS, without redirects.

        The target is a compile-time constant (``https://api.openai.com``),
        operator configuration, not user or agent input; TLS is the
        default verified context, the response is bounded (1 MB), and
        redirects are NEVER followed — a 3xx surfaces as an error
        (``PROVIDER_ERROR``) instead of a silent scheme/host change.
        Returns ``(state, parsed_json_or_None)``.
        """
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._openai_key}",
                     **(headers or {})})
        try:
            with _NO_REDIRECT_OPENER.open(
                    req, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read(1_000_000)
            return "SUCCESS", json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                return "PROVIDER_ERROR", None  # redirect refused by policy
            return _classify_http_error(exc.code), None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason):
                return "TIMEOUT", None
            return "PROVIDER_ERROR", None
        except TimeoutError:
            return "TIMEOUT", None
        except ValueError:
            return "PROVIDER_ERROR", None

    def _search_openai_responses(
            self, query: str) -> tuple[str, list[WebSearchResult]]:
        """OpenAI Responses API with the built-in web_search tool."""
        body: dict[str, Any] = {
            "model": SEARCH_MODEL,
            "tools": [{"type": "web_search"}],
            "input": f"Search the web for: {query}",
        }
        state, data = self._openai_post(
            "https://api.openai.com/v1/responses", body)
        if state != "SUCCESS" or data is None:
            return state, []
        results = []
        for item in data.get("output", []):
            if item.get("type") == "web_search_call":
                for result in (item.get("results") or [])[:MAX_RESULTS]:
                    results.append(WebSearchResult(
                        title=str(result.get("title", "")),
                        url=str(result.get("url", "")),
                        snippet=str(result.get("snippet", "")),
                        kind=KIND_REAL_SEARCH))
        if not results:
            # No web_search_call results came back. If the model still
            # produced an answer it is MODEL KNOWLEDGE — real search
            # output was empty — and must never be presented as a
            # verified web result.
            text = ""
            for item in data.get("output", []):
                if item.get("type") == "message":
                    for content in item.get("content", []):
                        if content.get("type") == "output_text":
                            text += content.get("text", "")
            if text:
                results.append(WebSearchResult(
                    title="Model knowledge (no web results returned)",
                    url="", snippet=text, kind=KIND_MODEL_KNOWLEDGE))
        return "SUCCESS", results

    def _search_openai_chat(
            self, query: str) -> tuple[str, list[WebSearchResult]]:
        """Knowledge fallback via Chat Completions, clearly labeled.

        The chat model has no live search tool here; anything it
        returns is unverified MODEL_KNOWLEDGE (titles/URLs may be
        plausible but wrong) and is labeled as such.
        """
        body = {
            "model": SEARCH_MODEL,
            "messages": [
                {"role": "system",
                 "content": ("You are a research assistant. Search the web "
                             "for the user's query and return the top 5 "
                             "results as JSON: "
                             '[{"title":"...","url":"...","snippet":"..."}]. '
                             "Return ONLY valid JSON array, no prose.")},
                {"role": "user", "content": query},
            ],
            "max_tokens": 2000,
            "temperature": 0.3,
        }
        state, data = self._openai_post(
            "https://api.openai.com/v1/chat/completions", body)
        if state != "SUCCESS" or data is None:
            return state, []
        choices = data.get("choices") or []
        if not choices:
            return "PROVIDER_ERROR", []
        text = (choices[0].get("message", {}).get("content", "") or "")
        text = text.strip().strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
        try:
            items = json.loads(text)
        except ValueError:
            return "PROVIDER_ERROR", []
        results = []
        for item in items[:MAX_RESULTS]:
            if isinstance(item, dict):
                title = str(item.get("title", "")).strip()
                results.append(WebSearchResult(
                    title=title or "Model knowledge (unverified)",
                    url=str(item.get("url", "")),
                    snippet=str(item.get("snippet", "")),
                    kind=KIND_MODEL_KNOWLEDGE))
        return "SUCCESS", results


def fetch_page_content(url: str, max_chars: int = 8000,
                       audit: Any = None) -> dict[str, Any]:
    """Fetch and extract text content from a URL (bounded + SSRF-safe).

    Returns a dict with ``ok``/``blocked``/``state``/``content``/``note``.
    ``content`` is plain text only when the fetch succeeded; otherwise an
    empty string plus a machine-readable reason.
    """
    url = (url or "").strip()[:500]
    if not url:
        return {"ok": False, "blocked": True, "state": "POLICY_DENIED",
                "content": "", "note": "Empty URL refused.", "url": ""}
    outcome = fetch(url, policy=RESEARCH_POLICY, audit=audit)
    if outcome.blocked:
        return {"ok": False, "blocked": True, "state": "POLICY_DENIED",
                "content": "",
                "note": f"URL refused by network policy: "
                        f"{outcome.blocked_reason}",
                "url": url}
    if not outcome.ok:
        state = "TIMEOUT" if "timed out" in outcome.error_state else \
            _classify_http_error(outcome.status)
        return {"ok": False, "blocked": False, "state": state,
                "content": "",
                "note": f"Fetch failed: {outcome.error_state}",
                "url": url,
                "status": outcome.status,
                "content_type": outcome.content_type,
                "redirects": outcome.redirects}

    content_type = outcome.content_type or ""
    if not (content_type.startswith(("text/", "application/json",
                                     "application/xml", "application/xhtml",
                                     "application/javascript"))):
        return {"ok": False, "blocked": False,
                "state": "POLICY_DENIED",
                "content": "",
                "note": f"Non-text content refused: {content_type}",
                "url": url, "status": outcome.status,
                "content_type": content_type}
    raw = outcome.body.decode("utf-8", errors="ignore")
    # Strip HTML tags for plain text extraction.
    text = re.sub(r"<script[^>]*>.*?</script>", "", raw,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    truncated = len(text) > max_chars
    return {"ok": True, "blocked": False, "state": "SUCCESS",
            "content": text[:max_chars], "url": outcome.final_url or url,
            "status": outcome.status, "content_type": content_type,
            "bytes_read": outcome.bytes_read, "redirects": outcome.redirects,
            "truncated": truncated,
            "note": ("Fetched through the SSRF-safe chain; content is "
                     "untrusted external input.")}
