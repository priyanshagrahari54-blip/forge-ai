"""A44 provider-state tests (Phase 2/11 hardening).

A failed provider request must NEVER look like a successful model
response: every result carries an explicit state and
``ok == (state == SUCCESS)``, failures leave ``content`` empty, and
retries are bounded. All HTTP behavior is mocked — no network, and no
API key is required to prove the classification.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from forge.collaboration.openai_connector import OpenAIConnector
from forge.security.provider_states import (
    PROVIDER_HEALTH_STATES,
    PROVIDER_STATES,
    ProviderHealth,
    ProviderState,
    classify_http_status,
)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.openai.com/v1/chat/completions", code,
        "reason", {}, io.BytesIO(b"{}"))


def _ok_response(content: str = "real answer"):
    payload = json.dumps({"choices": [
        {"message": {"content": content}}]}).encode()
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, *a):
            return payload

    return _Resp()


@pytest.fixture()
def connector(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return OpenAIConnector(api_key="sk-test-key-0123456789abcdefghijkl",
                           retry_backoff=0)


# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------

def test_vocabularies_are_explicit():
    assert list(ProviderState.__members__) == [
        "SUCCESS", "PROVIDER_ERROR", "TIMEOUT", "RATE_LIMITED",
        "AUTH_ERROR", "POLICY_DENIED", "UNAVAILABLE"]
    assert list(ProviderHealth.__members__) == [
        "AVAILABLE", "DEGRADED", "RATE_LIMITED", "AUTH_FAILED",
        "UNAVAILABLE", "MISCONFIGURED"]
    assert PROVIDER_STATES and PROVIDER_HEALTH_STATES


@pytest.mark.parametrize("status,expected", [
    (200, "PROVIDER_ERROR"),   # 2xx without content handled elsewhere
    (400, "PROVIDER_ERROR"),
    (401, "AUTH_ERROR"),
    (403, "AUTH_ERROR"),
    (408, "TIMEOUT"),
    (429, "RATE_LIMITED"),
    (500, "PROVIDER_ERROR"),
    (503, "PROVIDER_ERROR"),
    (None, "PROVIDER_ERROR"),
])
def test_http_status_classification(status, expected):
    assert classify_http_status(status).value == expected


# ---------------------------------------------------------------------------
# missing key → UNAVAILABLE, never success
# ---------------------------------------------------------------------------

def test_no_key_is_unavailable_not_success(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    connector = OpenAIConnector()
    result = connector.ask("hi")
    assert result["state"] == "UNAVAILABLE"
    assert result["ok"] is False
    assert result["content"] == ""
    assert result["simulation"] is False
    assert "No OPENAI_API_KEY" in result["error"]
    assert connector.health()["status"] == "UNAVAILABLE"


def test_health_reports_configured_available(connector):
    health = connector.health()
    assert health["status"] == "AVAILABLE"


# ---------------------------------------------------------------------------
# mocked HTTP outcomes
# ---------------------------------------------------------------------------

def test_success_state(connector, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: _ok_response())  # noqa: E501
    result = connector.ask("question")
    assert result["state"] == "SUCCESS"
    assert result["ok"] is True
    assert result["content"] == "real answer"
    assert result["untrusted"] is True
    assert result["source"] == "external_ai"


def test_auth_error_is_explicit(connector, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout: (_ for _ in ()).throw(
                            _http_error(401)))
    result = connector.ask("question")
    assert result["state"] == "AUTH_ERROR"
    assert result["ok"] is False
    assert result["content"] == ""
    assert "HTTP 401" in result["error"]
    assert result["simulation"] is False


def test_rate_limited_is_explicit(connector, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout: (_ for _ in ()).throw(
                            _http_error(429)))
    result = connector.ask("question")
    assert result["state"] == "RATE_LIMITED"
    assert result["ok"] is False
    assert result["content"] == ""


def test_provider_error_is_explicit(connector, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout: (_ for _ in ()).throw(
                            _http_error(500)))
    result = connector.ask("question")
    assert result["state"] == "PROVIDER_ERROR"
    assert result["ok"] is False
    assert result["content"] == ""


def test_retries_are_bounded(connector, monkeypatch):
    """429/5xx get one retry; a persistent failure still reports the
    classified state instead of pretending success."""
    calls = {"n": 0}

    def flaky(req, timeout):
        calls["n"] += 1
        if calls["n"] < 3:  # 2 attempts = initial + 1 retry
            raise _http_error(429)
        return _ok_response()

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    result = connector.ask("question")
    assert calls["n"] == 2
    assert result["state"] == "RATE_LIMITED"
    assert result["ok"] is False


def test_timeout_is_explicit(connector, monkeypatch):
    def timeout_error(req, timeout):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", timeout_error)
    result = connector.ask("question")
    assert result["state"] == "TIMEOUT"
    assert result["ok"] is False


def test_failure_never_leaks_the_api_key(connector, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout: (_ for _ in ()).throw(
                            _http_error(401)))
    result = connector.ask("question")
    serialized = json.dumps(result)
    assert "sk-test-key" not in serialized
    assert "sk-test-key" not in result["error"]
    # Request header carries the key, but no log/result does.
    captured = {}

    def spy(req, timeout):
        captured["auth"] = req.headers.get("Authorization", "")
        raise _http_error(401)

    monkeypatch.setattr(urllib.request, "urlopen", spy)
    connector.ask("question")
    assert captured["auth"] == "Bearer sk-test-key-0123456789abcdefghijkl"


def test_empty_content_is_provider_error(connector, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout: _ok_response(""))
    result = connector.ask("question")
    assert result["state"] == "PROVIDER_ERROR"
    assert result["ok"] is False
    assert result["content"] == ""


def test_unknown_connector_still_fails_closed():
    from forge.collaboration.connectors import build_connector
    with pytest.raises(ValueError):
        build_connector("some-other-ai")
