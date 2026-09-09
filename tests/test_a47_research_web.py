"""A47 web-research provider hardening tests.

Truthfulness guarantees, all offline (no network, no secrets):

- Only real provider output is ever a REAL_SEARCH_RESULT; knowledge
  fallbacks and text-only answers are MODEL_KNOWLEDGE.
- Provider errors never become fake results, and no fallback happens
  on auth/rate/timeout/unavailable failures.
- OpenAI API POSTs never follow redirects (fixed host, verified TLS).
- fetch_page_content classifies blocked/timed-out/refused outcomes.
- ResearchEngine evidence carries provenance and honest answers for
  empty result sets.
"""
from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest  # noqa: E402

from forge.research import web as research_web  # noqa: E402
from forge.research.engine import ResearchEngine  # noqa: E402
from forge.research.web import (  # noqa: E402
    KIND_MODEL_KNOWLEDGE,
    KIND_REAL_SEARCH,
    WebSearchProvider,
    WebSearchResult,
    fetch_page_content,
)

_OPENAI_KEY = "sk-test-not-a-real-key-1234567890"


class _Outcome:
    """Minimal stand-in for forge.security.ssrf.FetchOutcome."""

    def __init__(self, *, ok: bool = True, blocked: bool = False,
                 status: int = 200, content_type: str = "text/html",
                 body: bytes = b"", final_url: str = "",
                 error_state: str = "", blocked_reason: str = "",
                 bytes_read: int = 0, redirects: int = 0) -> None:
        self.ok = ok
        self.blocked = blocked
        self.status = status
        self.content_type = content_type
        self.body = body
        self.final_url = final_url or "https://example.org/page"
        self.error_state = error_state
        self.blocked_reason = blocked_reason
        self.bytes_read = bytes_read or len(body)
        self.redirects = redirects


def _clean_env(monkeypatch) -> None:  # noqa: ANN001
    for key in ("OPENAI_API_KEY", "FORGE_SEARXNG_URL",
                "FORGE_SEARXNG_ALLOW_HTTP", "FORGE_SEARXNG_ALLOW_PRIVATE"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    _clean_env(monkeypatch)


# ---------------------------------------------------------------------------
# provenance labels
# ---------------------------------------------------------------------------

def test_searxng_results_are_real_search_results(monkeypatch) -> None:
    body = json.dumps({"results": [
        {"title": "First", "url": "https://example.org/1",
         "content": "snippet one"},
        {"title": "Second", "url": "https://example.org/2",
         "content": "snippet two"},
    ]}).encode("utf-8")
    outcome = _Outcome(content_type="application/json", body=body)
    monkeypatch.setattr(research_web, "fetch",
                        lambda url, policy=None, audit=None: outcome)
    monkeypatch.setenv("FORGE_SEARXNG_URL", "https://search.example.org")
    provider = WebSearchProvider()
    state, results = provider.search("forge")
    assert state == "SUCCESS"
    assert results and all(r.kind == KIND_REAL_SEARCH for r in results)
    assert results[0].to_dict()["kind"] == KIND_REAL_SEARCH


def test_openai_web_search_results_are_real(monkeypatch) -> None:
    payload = {"output": [
        {"type": "web_search_call", "results": [
            {"title": "Hit", "url": "https://example.org/hit",
             "snippet": "snip"}]},
    ]}

    def fake_post(url, body, headers=None):  # noqa: ANN001
        assert url == "https://api.openai.com/v1/responses"
        return "SUCCESS", payload

    monkeypatch.setenv("OPENAI_API_KEY", _OPENAI_KEY)
    provider = WebSearchProvider()
    monkeypatch.setattr(provider, "_openai_post", fake_post)
    state, results = provider.search("forge")
    assert state == "SUCCESS"
    assert len(results) == 1
    assert results[0].kind == KIND_REAL_SEARCH
    assert results[0].url == "https://example.org/hit"


def test_openai_text_only_answer_is_model_knowledge(monkeypatch) -> None:
    """Zero web_search_call results + an answer text = MODEL_KNOWLEDGE,
    never a disguised search result."""
    payload = {"output": [
        {"type": "message", "content": [
            {"type": "output_text",
             "text": "I could not run a live search, but I know that "
                     "Forge is a coding agent."}]},
    ]}

    def fake_post(url, body, headers=None):  # noqa: ANN001
        return "SUCCESS", payload

    monkeypatch.setenv("OPENAI_API_KEY", _OPENAI_KEY)
    provider = WebSearchProvider()
    monkeypatch.setattr(provider, "_openai_post", fake_post)
    state, results = provider.search("what is forge")
    assert state == "SUCCESS"
    assert len(results) == 1
    assert results[0].kind == KIND_MODEL_KNOWLEDGE
    assert results[0].url == ""
    assert "Model knowledge" in results[0].title
    assert results[0].is_real is False


def test_chat_knowledge_fallback_is_model_knowledge(monkeypatch) -> None:
    """Generic provider error on the Responses path falls back to chat
    knowledge — which must be labeled MODEL_KNOWLEDGE."""
    chat_items = [{"title": "Maybe", "url": "https://example.org/x",
                   "snippet": "plausible but unverified"}]
    chat_data = {"choices": [{"message": {
        "content": json.dumps(chat_items)}}]}

    calls = []

    def fake_post(url, body, headers=None):  # noqa: ANN001
        calls.append(url)
        if url.endswith("/responses"):
            return "PROVIDER_ERROR", None
        return "SUCCESS", chat_data

    monkeypatch.setenv("OPENAI_API_KEY", _OPENAI_KEY)
    provider = WebSearchProvider()
    monkeypatch.setattr(provider, "_openai_post", fake_post)
    state, results = provider.search("forge")
    assert state == "SUCCESS"
    assert calls == ["https://api.openai.com/v1/responses",
                     "https://api.openai.com/v1/chat/completions"]
    assert results and all(r.kind == KIND_MODEL_KNOWLEDGE
                           for r in results)


@pytest.mark.parametrize("state", ["UNAVAILABLE", "AUTH_ERROR",
                                   "RATE_LIMITED", "TIMEOUT"])
def test_no_knowledge_fallback_on_recurring_failures(monkeypatch,
                                                     state: str) -> None:
    """Auth/rate/timeout/unavailable must NOT trigger the knowledge
    fallback (it would hide a real provider failure)."""
    monkeypatch.setenv("OPENAI_API_KEY", _OPENAI_KEY)
    provider = WebSearchProvider()

    def responses_stub(query):  # noqa: ANN001
        return state, []

    def chat_boom(query):  # noqa: ANN001
        raise AssertionError("knowledge fallback must not run")

    monkeypatch.setattr(provider, "_search_openai_responses",
                        responses_stub)
    monkeypatch.setattr(provider, "_search_openai_chat", chat_boom)
    got_state, results = provider.search("forge")
    assert got_state == state
    assert results == []


def test_empty_success_is_honest_not_fake_error(monkeypatch) -> None:
    """A provider that answers with zero usable results returns
    SUCCESS + [] — callers say so instead of inventing results."""
    monkeypatch.setenv("OPENAI_API_KEY", _OPENAI_KEY)
    provider = WebSearchProvider()

    def responses_stub(query):  # noqa: ANN001
        return "SUCCESS", []

    monkeypatch.setattr(provider, "_search_openai_responses",
                        responses_stub)
    state, results = provider.search("forge")
    assert state == "SUCCESS"
    assert results == []


# ---------------------------------------------------------------------------
# OpenAI API transport: fixed host, verified TLS, no redirects
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self, _n: int = -1) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def test_openai_post_success_parses_json(monkeypatch) -> None:
    captured: dict = {}

    class _FakeOpener:
        def open(self, request, timeout=None):  # noqa: ANN001
            captured["url"] = request.full_url
            captured["method"] = request.method
            captured["auth"] = request.headers.get("Authorization")
            return _FakeResponse(json.dumps({"ok": 1}).encode())

    monkeypatch.setattr(research_web, "_NO_REDIRECT_OPENER", _FakeOpener())
    monkeypatch.setenv("OPENAI_API_KEY", _OPENAI_KEY)
    provider = WebSearchProvider()
    state, data = provider._openai_post(
        "https://api.openai.com/v1/responses", {"model": "x"})
    assert state == "SUCCESS"
    assert data == {"ok": 1}
    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["method"] == "POST"
    assert captured["auth"] == f"Bearer {_OPENAI_KEY}"


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_openai_post_never_follows_redirects(monkeypatch, code: int) -> None:
    """A redirect is an error, never a silent host/scheme change."""
    calls: list = []

    class _RedirectOpener:
        def open(self, request, timeout=None):  # noqa: ANN001
            calls.append(request.full_url)
            raise urllib.error.HTTPError(
                request.full_url, code, "redirect", {}, None)

    monkeypatch.setattr(research_web, "_NO_REDIRECT_OPENER",
                        _RedirectOpener())
    monkeypatch.setenv("OPENAI_API_KEY", _OPENAI_KEY)
    provider = WebSearchProvider()
    state, data = provider._openai_post(
        "https://api.openai.com/v1/responses", {"model": "x"})
    assert state == "PROVIDER_ERROR"
    assert data is None
    assert len(calls) == 1  # exactly one attempt, no follow-up


# ---------------------------------------------------------------------------
# fetch_page_content classification
# ---------------------------------------------------------------------------

def test_fetch_page_content_success_strips_html(monkeypatch) -> None:
    html = b"<html><head><title>x</title></head>" \
           b"<body><h1>Hello</h1><p>World</p><script>bad()</script></body>"
    outcome = _Outcome(content_type="text/html", body=html,
                       final_url="https://example.org/page?q=1")
    monkeypatch.setattr(research_web, "fetch",
                        lambda url, policy=None, audit=None: outcome)
    result = fetch_page_content("https://example.org/page?q=1")
    assert result["ok"] is True
    assert result["state"] == "SUCCESS"
    assert "Hello" in result["content"] and "World" in result["content"]
    assert "bad()" not in result["content"]
    assert result["url"] == "https://example.org/page?q=1"


def test_fetch_page_content_blocked_is_policy_denied(monkeypatch) -> None:
    outcome = _Outcome(ok=False, blocked=True,
                       blocked_reason="loopback address 127.0.0.1")
    monkeypatch.setattr(research_web, "fetch",
                        lambda url, policy=None, audit=None: outcome)
    result = fetch_page_content("https://127.0.0.1/")
    assert result["ok"] is False
    assert result["blocked"] is True
    assert result["state"] == "POLICY_DENIED"
    assert "loopback" in result["note"]


def test_fetch_page_content_timeout_state(monkeypatch) -> None:
    outcome = _Outcome(ok=False, blocked=False, status=0,
                       error_state="request timed out after 15s")
    monkeypatch.setattr(research_web, "fetch",
                        lambda url, policy=None, audit=None: outcome)
    result = fetch_page_content("https://example.org/slow")
    assert result["ok"] is False
    assert result["state"] == "TIMEOUT"


def test_fetch_page_content_http_error_state(monkeypatch) -> None:
    outcome = _Outcome(ok=False, blocked=False, status=429,
                       error_state="http 429")
    monkeypatch.setattr(research_web, "fetch",
                        lambda url, policy=None, audit=None: outcome)
    result = fetch_page_content("https://example.org/limited")
    assert result["ok"] is False
    assert result["state"] == "RATE_LIMITED"


# ---------------------------------------------------------------------------
# engine provenance
# ---------------------------------------------------------------------------

def _make_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "helpers.py").write_text("def util(): return 1\n")
    (root / "core.py").write_text(
        "import helpers\n\nclass Engine:\n"
        "    def run(self):\n        return helpers.util()\n")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_core.py").write_text(
        "from core import Engine\n\ndef test_run():\n"
        "    assert Engine().run() == 1\n")


class _FakeProvider:
    provider_name = "fake-openai"

    def __init__(self, state: str, results: list):
        self._state = state
        self._results = results

    def available(self) -> bool:
        return True

    def search(self, query: str):  # noqa: ANN001
        return self._state, self._results


def test_ask_web_evidence_keeps_provenance(tmp_path, monkeypatch) -> None:
    _make_repo(tmp_path)
    results = [
        WebSearchResult(title="Real", url="https://example.org/1",
                        snippet="real snippet"),
        WebSearchResult(title="Knowledge", url="", snippet="unverified",
                        kind=KIND_MODEL_KNOWLEDGE),
    ]
    monkeypatch.setattr(research_web, "WebSearchProvider",
                        lambda: _FakeProvider("SUCCESS", results))
    engine = ResearchEngine(tmp_path)
    answer = engine.ask_web("what is forge?")
    assert answer["state"] == "SUCCESS"
    kinds = {item["kind"] for item in answer["evidence"]}
    assert kinds == {"web_result", "model_knowledge"}, kinds
    by_kind = {item["kind"]: item for item in answer["evidence"]}
    assert by_kind["model_knowledge"]["url"] == ""
    assert by_kind["model_knowledge"]["source_kind"] == KIND_MODEL_KNOWLEDGE
    assert by_kind["web_result"]["source_kind"] == KIND_REAL_SEARCH
    assert "model-knowledge" in answer["answer"]
    assert "live web result" in answer["answer"]


def test_ask_web_empty_success_is_honest(tmp_path, monkeypatch) -> None:
    _make_repo(tmp_path)
    monkeypatch.setattr(research_web, "WebSearchProvider",
                        lambda: _FakeProvider("SUCCESS", []))
    engine = ResearchEngine(tmp_path)
    answer = engine.ask_web("nothing to find")
    assert answer["state"] == "SUCCESS"
    assert answer["evidence"] == []
    assert "no results" in answer["answer"]
    assert "will not guess" in answer["answer"]
