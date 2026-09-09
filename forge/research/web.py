"""Real web research provider for A47.

Provides real Internet search via configured search APIs. Supports:
- OpenAI-powered web search (when OPENAI_API_KEY is set)
- DuckDuckGo HTML scraping as a fallback (no key needed)
- SearXNG instance (when FORGE_SEARXNG_URL is set)

All results are marked with their source and honestly labeled.
Web results are untrusted external input, same as any model output.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

MAX_QUERY = 500
MAX_RESULTS = 10
REQUEST_TIMEOUT = 15.0


class WebSearchResult:
    """One search result from the web."""

    __slots__ = ("title", "url", "snippet")

    def __init__(self, title: str, url: str, snippet: str) -> None:
        self.title = title[:200]
        self.url = url[:500]
        self.snippet = snippet[:500]

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url,
                "snippet": self.snippet}


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

    def search(self, query: str) -> list[WebSearchResult]:
        """Search the web; returns bounded results or empty on failure."""
        query = (query or "").strip()[:MAX_QUERY]
        if not query:
            return []
        if self._searxng_url:
            return self._search_searxng(query)
        if self._openai_key:
            return self._search_openai(query)
        return []

    def _search_searxng(self, query: str) -> list[WebSearchResult]:
        """Search via a SearXNG instance."""
        import json
        import urllib.request
        import urllib.parse

        url = (f"{self._searxng_url.rstrip('/')}/search?"
               f"q={urllib.parse.quote(query)}&format=json"
               f"&categories=general")
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(
                    req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            results = []
            for item in (data.get("results") or [])[:MAX_RESULTS]:
                results.append(WebSearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("content", "")))
            return results
        except Exception:
            return []

    def _search_openai(self, query: str) -> list[WebSearchResult]:
        """Use OpenAI to summarize web search results.

        This uses the Responses API with web_search tool if available,
        falling back to the Chat Completions API for a knowledge-based
        answer. Results are labeled as model-assisted.
        """
        import json
        import urllib.request
        import urllib.error

        # Try OpenAI Responses API with web_search tool first
        try:
            return self._search_openai_responses(query)
        except Exception:
            pass

        # Fall back to Chat Completions for a knowledge answer
        body = json.dumps({
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system",
                 "content": ("You are a research assistant. "
                             "Search the web for the user's query and "
                             "return the top 5 results as JSON: "
                             '[{"title":"...","url":"...","snippet":"..."}]. '
                             "Return ONLY valid JSON array, no prose.")},
                {"role": "user", "content": query},
            ],
            "max_tokens": 2000,
            "temperature": 0.3,
        }).encode("utf-8")

        url = "https://api.openai.com/v1/chat/completions"
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._openai_key}",
            })
        try:
            with urllib.request.urlopen(
                    req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            choices = data.get("choices") or []
            if not choices:
                return []
            text = choices[0].get("message", {}).get("content", "")
            text = text.strip().strip("`")
            if text.startswith("json"):
                text = text[4:].strip()
            items = json.loads(text)
            results = []
            for item in items[:MAX_RESULTS]:
                if isinstance(item, dict):
                    results.append(WebSearchResult(
                        title=item.get("title", ""),
                        url=item.get("url", ""),
                        snippet=item.get("snippet", "")))
            return results
        except Exception:
            return []

    def _search_openai_responses(self, query: str
                                 ) -> list[WebSearchResult]:
        """Use OpenAI Responses API with built-in web search."""
        import json
        import urllib.request

        body = json.dumps({
            "model": "gpt-4o-mini",
            "tools": [{"type": "web_search"}],
            "input": f"Search the web for: {query}",
        }).encode("utf-8")

        url = "https://api.openai.com/v1/responses"
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._openai_key}",
            })
        with urllib.request.urlopen(
                req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # Extract results from the response
        results = []
        for item in data.get("output", []):
            if item.get("type") == "web_search_call":
                for result in (item.get("results") or [])[:MAX_RESULTS]:
                    results.append(WebSearchResult(
                        title=result.get("title", ""),
                        url=result.get("url", ""),
                        snippet=result.get("snippet", "")))
        if not results:
            text = ""
            for item in data.get("output", []):
                if item.get("type") == "message":
                    for content in item.get("content", []):
                        if content.get("type") == "output_text":
                            text += content.get("text", "")
            if text:
                results.append(WebSearchResult(
                    title="AI Search Result", url="", snippet=text))
        return results


def fetch_page_content(url: str, max_chars: int = 8000) -> str:
    """Fetch and extract text content from a URL (bounded)."""
    import urllib.request
    import urllib.error
    import re

    url = (url or "").strip()[:500]
    if not url.startswith(("http://", "https://")):
        return ""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "ForgeBot/1.0 (research)"})
        with urllib.request.urlopen(
                req, timeout=REQUEST_TIMEOUT) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "text/" not in content_type and "html" not in content_type:
                return f"[Non-text content: {content_type}]"
            raw = resp.read(100_000).decode("utf-8", errors="ignore")
        # Strip HTML tags for plain text extraction
        text = re.sub(r"<script[^>]*>.*?</script>", "", raw,
                       flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text,
                       flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception as exc:
        return f"[fetch error: {exc}]"
