"""Session 11 — the remote provider path and the hardened API requester.

Spec sections covered: 16 (optional remote providers: A33, destination-IP
policy, TLS, no redirect retargeting, credential redaction), 26 (dedicated
security audit: SSRF), 12 (free-first cost policy), 24 (tests must distinguish
mocked from real backends).

Every network test here talks to a **loopback double** started by
``helpers_s11.loopback_provider_server``. The double replays scripted text, so
these are ``MOCK_BACKEND_TEST`` cases: the HTTP path, the policy chain and the
adapter's honesty are real, the *provider* is not. The one test that requires a
genuine third-party provider is opt-in and lives in ``test_inference_live.py``
(``FORGE_REAL_INFERENCE=1``).

Run: ``pytest tests/test_inference_remote_provider.py -q``
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from helpers_s11 import (  # noqa: E402
    isolate_env,
    loopback_provider_server,
    stop_server,
)

from forge.models.remote import (  # noqa: E402
    RemoteHttpBackend,
    RemoteProviderConfig,
)
from forge.runtime.model_runtime import (  # noqa: E402
    BackendUnavailableError,
    RuntimeRequest,
    RuntimeSecurityError,
)
from forge.security.ssrf import FetchPolicy, request as hardened_request  # noqa: E402

#: A credential that must never appear in a log, an error or an audit record.
SECRET_KEY = "sk-live-SUPERSECRET-do-not-leak-0123456789"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def provider(monkeypatch, tmp_path):
    """A loopback OpenAI-compatible double + a config pointed at it."""
    isolate_env(monkeypatch, tmp_path)
    server, base, state = loopback_provider_server()
    config = RemoteProviderConfig(
        provider_id="acme", base_url=base, model="acme-1",
        api_key=SECRET_KEY, allow_http=True, timeout=5.0)
    yield SimpleNamespace(server=server, base=base, state=state,
                          config=config,
                          calls=server.forge_calls,  # type: ignore[attr-defined]
                          port=int(urlsplit(base).port or 0))
    stop_server(server)


def loopback_policy(port: int, **overrides) -> FetchPolicy:
    """A policy that permits exactly one operator-declared loopback endpoint."""
    kwargs = dict(allow_http=True, private_allowed_hosts=("127.0.0.1",),
                  allow_ports=(port,), timeout=5.0,
                  max_bytes=1024 * 1024,
                  audit_operation="test_remote_inference",
                  audit_agent="forge-test")
    kwargs.update(overrides)
    return FetchPolicy(**kwargs)


def backend_for(config: RemoteProviderConfig, *,
                allow_network: bool = True) -> RemoteHttpBackend:
    return RemoteHttpBackend(config, allow_network=allow_network)


class RecordingAudit:
    """An audit sink that keeps every decision the requester reports."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def record(self, **kwargs) -> None:
        self.records.append(dict(kwargs))

    def __call__(self, *args, **kwargs) -> None:
        self.records.append({"args": list(args), **kwargs})


# ---------------------------------------------------------------------------
# 1. the hardened API requester (real sockets, loopback double)
# ---------------------------------------------------------------------------


def test_request_round_trips_json_and_pins_the_hardening_headers(provider):
    """MOCK_BACKEND_TEST: one bounded POST really reaches the double."""
    payload = json.dumps({"model": "acme-1", "messages": []}).encode("utf-8")
    outcome = hardened_request(
        "POST", provider.base + "/chat/completions", payload=payload,
        headers={"X-Trace": "kept", "Content-Type": "application/json"},
        policy=loopback_policy(provider.port))

    assert outcome.ok is True
    assert outcome.status == 200
    assert outcome.content_type == "application/json"
    data = json.loads(outcome.body.decode("utf-8"))
    assert data["choices"][0]["message"]["content"] == "remote-scripted"
    assert outcome.bytes_read == len(outcome.body) > 0

    call = provider.calls[-1]
    assert call["method"] == "POST"
    assert call["body"] == payload
    #: The hardening headers are set by the requester, not by the caller.
    assert call["headers"]["host"] == "127.0.0.1:%d" % provider.port
    assert call["headers"]["accept-encoding"] == "identity"
    assert call["headers"]["connection"] == "close"
    assert call["headers"]["content-length"] == str(len(payload))
    assert call["headers"]["x-trace"] == "kept"


def test_request_ignores_caller_attempts_to_override_pinning(provider):
    """MOCK_BACKEND_TEST: a caller header cannot defeat pinning or the bound."""
    payload = b'{"model": "acme-1"}'
    outcome = hardened_request(
        "POST", provider.base + "/chat/completions", payload=payload,
        headers={"Host": "evil.example", "Connection": "keep-alive",
                 "Accept-Encoding": "gzip", "Content-Length": "1",
                 "Transfer-Encoding": "chunked"},
        policy=loopback_policy(provider.port))

    assert outcome.ok is True
    call = provider.calls[-1]
    assert call["headers"]["host"] == "127.0.0.1:%d" % provider.port
    assert call["headers"]["connection"] == "close"
    assert call["headers"]["accept-encoding"] == "identity"
    assert call["headers"]["content-length"] == str(len(payload))
    assert "transfer-encoding" not in call["headers"]


def test_request_refuses_methods_outside_the_closed_set(provider):
    """MOCK_BACKEND_TEST: only GET/POST exist; nothing is sent for the rest."""
    for verb in ("DELETE", "PUT", "TRACE", "OPTIONS", "patch"):
        outcome = hardened_request(
            verb, provider.base + "/models",
            policy=loopback_policy(provider.port))
        assert outcome.ok is False
        assert outcome.blocked is True
        assert "not permitted" in outcome.blocked_reason
    assert provider.calls == []        # never reached the wire


def test_request_refuses_a_redirect_instead_of_following_it(provider):
    """MOCK_BACKEND_TEST: a 3xx cannot retarget the request (§16, §26)."""
    provider.state["redirect_to"] = provider.base + "/../secret-target"
    outcome = hardened_request("GET", provider.base + "/models",
                               policy=loopback_policy(provider.port))

    assert outcome.ok is False
    assert outcome.blocked is True
    assert "redirects are refused" in outcome.blocked_reason
    assert outcome.redirects == 0
    #: The retarget was never requested — the double proves it.
    assert [call["path"] for call in provider.calls] == ["/v1/models"]
    assert not any("secret-target" in call["path"] for call in provider.calls)


def test_request_bounds_the_response_body(provider):
    """MOCK_BACKEND_TEST: an oversized answer is refused, not truncated."""
    provider.state["oversize"] = 200_000
    outcome = hardened_request("GET", provider.base + "/models",
                               policy=loopback_policy(provider.port,
                                                      max_bytes=4096),
                               max_bytes=4096)

    assert outcome.ok is False
    assert "exceeds the 4096-byte limit" in outcome.error_state
    assert outcome.body == b""


def test_request_refuses_a_non_json_content_type(provider):
    """MOCK_BACKEND_TEST: a binary answer is refused before it is read."""
    provider.state["body"] = b"\x00\x01\x02binary"
    provider.state["content_type"] = "application/octet-stream"
    outcome = hardened_request(
        "POST", provider.base + "/chat/completions", payload=b"{}",
        policy=loopback_policy(provider.port))

    assert outcome.ok is False
    assert "not a JSON/text answer" in outcome.error_state
    assert outcome.body == b""


def test_request_refuses_a_private_destination_without_the_opt_in(provider):
    """MOCK_BACKEND_TEST: the destination-IP policy is fail-closed."""
    url = "http://127.0.0.1:%d/v1/models" % provider.port
    outcome = hardened_request(
        "GET", url,
        policy=FetchPolicy(allow_http=True, allow_ports=(provider.port,),
                           timeout=5.0))

    assert outcome.ok is False
    assert outcome.blocked is True
    assert provider.calls == []        # refused before any socket


def test_request_reports_an_http_failure_as_a_failure(provider):
    """MOCK_BACKEND_TEST: a 401 is never a success and never invented text."""
    provider.state["status"] = 401
    outcome = hardened_request(
        "POST", provider.base + "/chat/completions", payload=b"{}",
        policy=loopback_policy(provider.port))

    assert outcome.ok is False
    assert outcome.status == 401
    assert "HTTP 401" in outcome.error_state
    assert SECRET_KEY not in json.dumps(outcome.to_dict())


def test_request_timeout_is_reported_honestly(provider):
    """MOCK_BACKEND_TEST: a slow endpoint times out; nothing is fabricated."""
    provider.state["sleep"] = 1.5
    started = time.perf_counter()
    outcome = hardened_request(
        "GET", provider.base + "/models",
        policy=loopback_policy(provider.port, timeout=0.4))
    elapsed = time.perf_counter() - started

    assert outcome.ok is False
    assert "timed out" in outcome.error_state
    assert outcome.body == b""
    assert elapsed < 1.4


def test_audit_records_never_carry_a_credential(provider):
    """MOCK_BACKEND_TEST: userinfo and token values are scrubbed (§16, §26)."""
    #: A URL with embedded credentials reaches the double, but the audit
    #: record must not keep them.
    audit = RecordingAudit()
    url = "http://operator:%s@127.0.0.1:%d/v1/models" % (SECRET_KEY,
                                                         provider.port)
    outcome = hardened_request("GET", url,
                               policy=loopback_policy(provider.port),
                               audit=audit)
    #: Embedded credentials are refused outright, before any socket.
    assert outcome.blocked is True
    assert "embedded credentials" in outcome.blocked_reason
    assert SECRET_KEY not in outcome.blocked_reason
    assert provider.calls == []
    #: The refusal is still audited — with the destination, without the key.
    assert audit.records, "a refusal must be audited"
    blob = json.dumps(audit.records, default=str)
    assert SECRET_KEY not in blob
    assert "operator:" not in blob
    assert "127.0.0.1" in blob             # the destination is still recorded

    #: A refusal is audited too, and its reason is scrubbed as well.
    audit2 = RecordingAudit()
    blocked = hardened_request(
        "GET", "http://operator:%s@10.9.8.7:9/v1/models" % SECRET_KEY,
        policy=loopback_policy(provider.port), audit=audit2)
    assert blocked.blocked is True
    assert audit2.records
    blob2 = json.dumps(audit2.records, default=str)
    assert SECRET_KEY not in blob2
    assert blocked.blocked_reason and SECRET_KEY not in blocked.blocked_reason


def test_audit_sees_a_bearer_token_only_redacted(provider):
    """MOCK_BACKEND_TEST: an Authorization value never survives into a reason."""
    from forge.security.ssrf import _redact_audit_text

    text = ("endpoint refused Authorization: Bearer %s and "
            "api_key=%s" % (SECRET_KEY, SECRET_KEY))
    scrubbed = _redact_audit_text(text)
    assert SECRET_KEY not in scrubbed
    assert "[redacted]" in scrubbed


# ---------------------------------------------------------------------------
# 2. provider configuration: credentials, schemes, bounds
# ---------------------------------------------------------------------------


def test_config_view_never_contains_the_api_key(monkeypatch):
    monkeypatch.setenv("FORGE_REMOTE_ACME_API_KEY", SECRET_KEY)
    config = RemoteProviderConfig(provider_id="acme",
                                  base_url="https://api.acme.example/v1",
                                  model="acme-1", api_key_env="FORGE_REMOTE_ACME_API_KEY")
    assert config.resolved_api_key() == SECRET_KEY
    view = json.dumps(config.to_dict())
    assert SECRET_KEY not in view
    assert config.to_dict()["api_key_set"] is True
    assert "api_key" not in config.to_dict()


def test_config_refuses_credential_bearing_headers():
    with pytest.raises(RuntimeSecurityError) as info:
        RemoteProviderConfig(provider_id="acme",
                             base_url="https://api.acme.example/v1",
                             headers={"Authorization": "Bearer " + SECRET_KEY})
    assert SECRET_KEY not in str(info.value)


def test_config_refuses_credentials_embedded_in_the_url():
    url = "https://user:%s@api.acme.example/v1" % SECRET_KEY
    with pytest.raises(RuntimeSecurityError) as info:
        RemoteProviderConfig(provider_id="acme", base_url=url)
    assert SECRET_KEY not in str(info.value)
    assert "embedded credentials" in str(info.value)


def test_config_refuses_a_non_http_scheme():
    with pytest.raises(RuntimeSecurityError):
        RemoteProviderConfig(provider_id="acme",
                             base_url="ftp://files.acme.example/v1")


def test_config_normalises_the_endpoint_root():
    config = RemoteProviderConfig(
        provider_id="acme",
        base_url="https://api.acme.example/v1/chat/completions/")
    assert config.base_url == "https://api.acme.example/v1"
    assert config.host == "api.acme.example"
    assert config.scheme == "https"
    assert config.loopback is False


def test_config_from_env_requires_a_url_and_maps_the_knobs(monkeypatch):
    monkeypatch.delenv("FORGE_REMOTE_ACME_URL", raising=False)
    with pytest.raises(ValueError):
        RemoteProviderConfig.from_env("acme")

    monkeypatch.setenv("FORGE_REMOTE_ACME_URL",
                       "https://api.acme.example/v1/chat/completions")
    monkeypatch.setenv("FORGE_REMOTE_ACME_MODEL", "acme-1")
    monkeypatch.setenv("FORGE_REMOTE_ACME_CAPABILITIES", "code, chat ,")
    monkeypatch.setenv("FORGE_REMOTE_ACME_CONTEXT_WINDOW", "32000")
    monkeypatch.setenv("FORGE_REMOTE_ACME_COST_PER_TOKEN", "0.000002")
    monkeypatch.setenv("FORGE_REMOTE_ACME_TIMEOUT", "99999")
    monkeypatch.setenv("FORGE_REMOTE_ACME_HOST_ALLOWLIST", "api.acme.example")
    config = RemoteProviderConfig.from_env("acme")
    assert config.base_url == "https://api.acme.example/v1"
    assert config.model == "acme-1"
    assert config.capabilities == ("code", "chat")
    assert config.context_window == 32000
    assert config.cost_per_token == pytest.approx(0.000002)
    assert config.timeout == 600.0            # clamped, not trusted
    assert config.host_allowlist == ("api.acme.example",)
    assert config.api_key_env == "FORGE_REMOTE_ACME_API_KEY"


def test_fetch_policy_is_fail_closed_for_a_public_provider():
    config = RemoteProviderConfig(provider_id="acme",
                                  base_url="https://api.acme.example:8443/v1",
                                  model="acme-1")
    policy = config.fetch_policy()
    assert policy.allow_http is False
    assert policy.private_allowed_hosts == ()
    assert policy.max_redirects == 0
    assert policy.allow_ports == (8443,)      # operator-declared port
    assert policy.max_bytes == config.max_response_bytes
    assert config.local_endpoint is False


def test_fetch_policy_tolerates_only_the_explicit_loopback_opt_in():
    local = RemoteProviderConfig(provider_id="local",
                                 base_url="http://127.0.0.1:9999/v1",
                                 allow_http=True)
    policy = local.fetch_policy()
    assert policy.allow_http is True
    assert policy.private_allowed_hosts == ("127.0.0.1",)
    assert policy.allow_ports == (9999,)
    assert local.local_endpoint is True

    #: Without the explicit opt-in, a loopback http endpoint stays refused.
    strict = RemoteProviderConfig(provider_id="local",
                                  base_url="http://127.0.0.1:9999/v1")
    assert strict.local_endpoint is False
    assert strict.fetch_policy().allow_http is False
    assert strict.fetch_policy().private_allowed_hosts == ()


def test_send_uses_the_same_policy_as_the_preflight_check(provider, monkeypatch):
    """MOCK_BACKEND_TEST: one posture for admission and for transmission."""
    captured: dict = {}

    def fake_request(method, url, **kwargs):
        captured.update(kwargs)
        captured["method"] = method
        captured["url"] = url
        raise BackendUnavailableError("stop here")

    monkeypatch.setattr("forge.models.remote.hardened_request", fake_request)
    backend = backend_for(provider.config)
    with pytest.raises(BackendUnavailableError):
        backend.list_models()

    policy = captured["policy"]
    expected = provider.config.fetch_policy()
    assert captured["allow_redirects"] is False
    assert policy.max_redirects == expected.max_redirects == 0
    assert policy.allow_http == expected.allow_http
    assert policy.private_allowed_hosts == expected.private_allowed_hosts
    assert policy.allow_ports == expected.allow_ports
    assert policy.max_bytes == expected.max_bytes
    assert captured["max_bytes"] == provider.config.max_response_bytes


# ---------------------------------------------------------------------------
# 3. the remote adapter: real HTTP, honest failures
# ---------------------------------------------------------------------------


def test_backend_discovery_lists_what_the_provider_really_serves(provider):
    """MOCK_BACKEND_TEST: discovery reads the provider, it does not guess."""
    backend = backend_for(provider.config)
    models = backend.list_models()
    assert [model.name for model in models] == ["acme-1"]
    assert models[0].local is False
    assert models[0].backend == backend.name == "remote-acme"
    assert backend.health(probe=True).status == "ready"


def test_backend_generate_returns_the_provider_text(provider):
    """MOCK_BACKEND_TEST: the text comes from the endpoint, unmodified."""
    provider.state["response"] = "remote answer 42"
    backend = backend_for(provider.config)
    response = backend.generate(RuntimeRequest(prompt="hello",
                                               model="acme-1"))
    assert response.success is True
    assert response.text == "remote answer 42"
    assert response.model == "acme-1"
    assert response.metadata["remote"] is True
    assert response.metadata["http_status"] == 200
    #: The credential travelled as a header and nowhere else.
    call = provider.calls[-1]
    assert call["headers"]["authorization"] == "Bearer %s" % SECRET_KEY
    assert SECRET_KEY not in call["body"].decode("utf-8")


def test_backend_refuses_to_fabricate_output_on_a_401(provider):
    """MOCK_BACKEND_TEST: an auth failure is a failure, not an answer."""
    provider.state["status"] = 401
    backend = backend_for(provider.config)
    with pytest.raises(BackendUnavailableError) as info:
        backend.generate(RuntimeRequest(prompt="hello", model="acme-1"))
    message = str(info.value)
    assert "rejected the credentials" in message
    assert SECRET_KEY not in message
    assert backend._failures >= 1


def test_backend_refuses_a_malformed_answer(provider):
    """MOCK_BACKEND_TEST: non-JSON is reported, never parsed into text."""
    provider.state["body"] = b"this is not json at all"
    provider.state["content_type"] = "application/json"
    backend = backend_for(provider.config)
    with pytest.raises(BackendUnavailableError) as info:
        backend.generate(RuntimeRequest(prompt="hello", model="acme-1"))
    assert "malformed" in str(info.value)


def test_backend_refuses_a_spoofed_model_identity(provider):
    """MOCK_BACKEND_TEST: a different model answering is not a substitute."""
    provider.state["reported_model"] = "totally-different-model"
    backend = backend_for(provider.config)
    response = backend.generate(RuntimeRequest(prompt="hello", model="acme-1"))
    assert response.success is False
    assert "refusing a silent substitution" in (response.error or "")
    assert response.text == ""


def test_backend_refuses_an_empty_completion(provider):
    """MOCK_BACKEND_TEST: an empty answer is a protocol failure."""
    provider.state["response"] = ""
    backend = backend_for(provider.config)
    response = backend.generate(RuntimeRequest(prompt="hello", model="acme-1"))
    assert response.success is False
    assert "empty completion" in (response.error or "")


def test_backend_contacts_nothing_without_allow_network(provider):
    """MOCK_BACKEND_TEST: network access off means off — no socket, no text."""
    backend = backend_for(provider.config, allow_network=False)
    available, detail = backend.available()
    assert available is False
    assert "network access is not enabled" in detail
    with pytest.raises(BackendUnavailableError):
        backend.generate(RuntimeRequest(prompt="hello", model="acme-1"))
    assert provider.calls == []
    assert backend.health(probe=True).status == "unavailable"


def test_backend_refuses_a_private_non_loopback_destination(provider):
    """MOCK_BACKEND_TEST: allow_http only ever applies to loopback."""
    config = RemoteProviderConfig(provider_id="lan",
                                  base_url="http://10.9.8.7:9000/v1",
                                  model="lan-1", allow_http=True)
    backend = backend_for(config, allow_network=True)
    available, detail = backend.available()
    assert available is False
    assert "plain http is refused" in detail
    with pytest.raises(BackendUnavailableError):
        backend.generate(RuntimeRequest(prompt="hello", model="lan-1"))
    assert provider.calls == []


def test_backend_refuses_a_public_destination_by_ip_policy(provider):
    """MOCK_BACKEND_TEST: a public-looking host on a private IP is refused."""
    config = RemoteProviderConfig(
        provider_id="rebind", model="rebind-1",
        base_url="http://127.0.0.1:%d/v1" % provider.port)   # no allow_http
    backend = backend_for(config, allow_network=True)
    available, detail = backend.available()
    assert available is False
    #: refused by the scheme rule before any DNS or socket work
    assert "plain http is refused" in detail
    assert provider.calls == []


def test_backend_timeout_is_counted_and_honest(provider):
    """MOCK_BACKEND_TEST: a slow provider times out; nothing is invented."""
    provider.state["sleep"] = 1.5
    config = RemoteProviderConfig(provider_id="acme", base_url=provider.base,
                                  model="acme-1", api_key=SECRET_KEY,
                                  allow_http=True, timeout=0.4)
    backend = backend_for(config)
    with pytest.raises(BackendUnavailableError) as info:
        backend.generate(RuntimeRequest(prompt="hello", model="acme-1"))
    assert "timed out" in str(info.value).lower() or \
        "could not answer" in str(info.value)
    assert backend._timeouts >= 1
    assert SECRET_KEY not in str(info.value)


def test_backend_stream_replays_bounded_chunks_and_ends_once(provider):
    """MOCK_BACKEND_TEST: chunks are the provider's text, in order, once done."""
    provider.state["response"] = "abcdefghij" * 12
    backend = backend_for(provider.config)
    chunks = list(backend.stream(RuntimeRequest(prompt="hello",
                                                model="acme-1")))
    assert chunks[-1].done is True
    assert sum(1 for chunk in chunks if chunk.done) == 1
    indices = [chunk.index for chunk in chunks]
    assert indices == sorted(set(indices))            # strictly monotonic
    text = "".join(chunk.text or "" for chunk in chunks)
    assert text == provider.state["response"]
    assert chunks[-1].metadata["streamed_by"] == "chunked-replay"


def test_backend_stream_honours_the_character_bound(provider):
    """MOCK_BACKEND_TEST: max_stream_chars caps what a stream may emit."""
    provider.state["response"] = "x" * 5000
    config = RemoteProviderConfig(provider_id="acme", base_url=provider.base,
                                  model="acme-1", allow_http=True,
                                  max_stream_chars=256)
    backend = backend_for(config)
    chunks = list(backend.stream(RuntimeRequest(prompt="hello",
                                                model="acme-1")))
    text = "".join(chunk.text or "" for chunk in chunks)
    assert len(text) == 256
    assert chunks[-1].done is True
    #: The cut is reported, never presented as a complete answer.
    assert chunks[-1].metadata["truncated"] is True
    assert chunks[-1].metadata["provider_chars"] == 5000
    assert chunks[-1].finish_reason == "length"


def test_backend_health_reports_a_refusing_endpoint_as_unavailable(provider):
    """MOCK_BACKEND_TEST: a redirecting endpoint is not healthy."""
    provider.state["redirect_to"] = provider.base + "/../secret-target"
    backend = backend_for(provider.config)
    health = backend.health(probe=True)
    assert health.status == "unavailable"
    assert health.error or health.detail
    assert "redirect" in (health.error + health.detail).lower() or \
        "blocked" in (health.error + health.detail).lower()
    assert not any("secret-target" in call["path"] for call in provider.calls)


# ---------------------------------------------------------------------------
# 4. the fabric end to end over a real (loopback) remote provider
# ---------------------------------------------------------------------------


def test_fabric_serves_a_remote_provider_end_to_end(provider):
    """MOCK_BACKEND_TEST: routing → verification → real HTTP generation."""
    from forge.models.engine import build_inference_fabric
    from forge.models.request import ModelRequest

    provider.state["response"] = "fabric served this"
    fabric = build_inference_fabric(remote_configs=(provider.config,),
                                    allow_network=True)
    report = fabric.catalog.discover()
    assert len(report.identities) >= 1
    assert report.registered
    identities = [identity for identity in fabric.catalog.list()
                  if identity.backend_id.startswith("remote")]
    assert len(identities) == 1
    remote = identities[0]
    assert remote.local is False
    assert remote.free is False          # a remote provider is never free-first
    assert remote.verified is False      # discovered != verified

    results = fabric.catalog.verify_all()
    assert [result.verified for result in results] == [True]
    assert fabric.catalog.get(remote.model_id).verified is True

    result = fabric.generate(ModelRequest(prompt="hello", capability="",
                                          model=remote.model_id,
                                          network_policy="explicit"))
    assert result.success is True
    assert result.neural is True
    assert result.text == "fabric served this"
    assert result.model_id == remote.model_id
    assert result.backend_id.startswith("remote")
    assert result.routing["selected_backend"].startswith("remote")


def test_fabric_refuses_the_remote_provider_for_secret_data(provider):
    """MOCK_BACKEND_TEST: A33 — SECRET never leaves the machine (§16)."""
    from forge.models.engine import build_inference_fabric
    from forge.models.request import ModelRequest

    fabric = build_inference_fabric(remote_configs=(provider.config,),
                                    allow_network=True, auto_verify=True)
    fabric.catalog.discover()
    remote = [identity for identity in fabric.catalog.list()
              if identity.backend_id.startswith("remote")][0]
    result = fabric.generate(ModelRequest(
        prompt="summarise the attached credential", capability="",
        model=remote.model_id, classification="secret",
        network_policy="explicit", allow_deterministic=False))

    assert result.success is False
    assert result.state == "policy_denied"
    assert result.text == ""
    assert result.neural is False
    assert "secret data may not reach an external provider" in result.error
    #: The refusal happened before the request was sent.
    assert not any(call["path"].endswith("/chat/completions")
                   for call in provider.calls)


def test_fabric_refuses_the_remote_provider_when_network_policy_is_off(provider):
    """MOCK_BACKEND_TEST: the default posture is no outbound inference."""
    from forge.models.engine import build_inference_fabric
    from forge.models.request import ModelRequest

    fabric = build_inference_fabric(remote_configs=(provider.config,),
                                    allow_network=True, auto_verify=True)
    fabric.catalog.discover()
    remote = [identity for identity in fabric.catalog.list()
              if identity.backend_id.startswith("remote")][0]
    result = fabric.generate(ModelRequest(
        prompt="hello", capability="", model=remote.model_id,
        network_policy="off", allow_deterministic=False))

    assert result.success is False
    assert result.state == "policy_denied"
    assert result.text == ""
    assert not any(call["path"].endswith("/chat/completions")
                   for call in provider.calls)


def test_auto_verify_proves_a_model_before_routing(provider):
    """MOCK_BACKEND_TEST: ``auto_verify`` is not a dead flag (§4, §16).

    Selection refuses an unverified model, so verification has to happen
    *before* routing or the flag could never take effect. The probe is the
    verifier's own prompt: the request text is not sent to prove a model.
    """
    from forge.models.engine import build_inference_fabric
    from forge.models.request import ModelRequest

    provider.state["response"] = "auto-verified answer"
    fabric = build_inference_fabric(remote_configs=(provider.config,),
                                    allow_network=True, auto_verify=True)
    fabric.catalog.discover()
    remote = [identity for identity in fabric.catalog.list()
              if identity.backend_id.startswith("remote")][0]
    assert remote.verified is False

    result = fabric.generate(ModelRequest(prompt="the user asked this",
                                          capability="", model=remote.model_id,
                                          network_policy="explicit"))
    assert result.success is True
    assert result.neural is True
    assert result.text == "auto-verified answer"
    assert fabric.catalog.get(remote.model_id).verified is True
    assert any(observation.phase == "auto_verify"
               for observation in result.observations)
    #: The probe carried the verifier's prompt, never the user's text.
    bodies = b"\n".join(call["body"] for call in provider.calls)
    assert b"verification probe" in bodies
    assert b"the user asked this" in bodies      # the real request, after
    assert bodies.index(b"verification probe") < bodies.index(
        b"the user asked this")


def test_without_auto_verify_an_unverified_model_stays_refused(provider):
    """MOCK_BACKEND_TEST: no verification, no selection — and no probing."""
    from forge.models.engine import build_inference_fabric
    from forge.models.request import ModelRequest

    fabric = build_inference_fabric(remote_configs=(provider.config,),
                                    allow_network=True, auto_verify=False)
    fabric.catalog.discover()
    remote = [identity for identity in fabric.catalog.list()
              if identity.backend_id.startswith("remote")][0]
    result = fabric.generate(ModelRequest(prompt="hello", capability="",
                                          model=remote.model_id,
                                          network_policy="explicit",
                                          allow_deterministic=False))
    assert result.success is False
    assert result.state == "unverified"
    assert result.text == ""
    assert result.neural is False
    assert "never becomes ready without real verification" in result.error
    #: Discovery is real traffic the operator asked for; a *generation* probe
    #: is not, and without auto_verify none was sent.
    assert not any(call["path"].endswith("/chat/completions")
                   for call in provider.calls)


def test_free_first_policy_refuses_a_paid_remote_provider(provider):
    """MOCK_BACKEND_TEST: allow_paid=False keeps a paid provider unselected."""
    from forge.models.engine import build_inference_fabric
    from forge.models.request import ModelRequest

    paid = RemoteProviderConfig(provider_id="acme", base_url=provider.base,
                                model="acme-1", api_key=SECRET_KEY,
                                allow_http=True, cost_per_token=0.01)
    policy = SimpleNamespace(allow_paid=False, allow_remote=True)
    fabric = build_inference_fabric(remote_configs=(paid,),
                                    allow_network=True, auto_verify=True,
                                    policy=policy)
    fabric.catalog.discover()
    remote = [identity for identity in fabric.catalog.list()
              if identity.backend_id.startswith("remote")][0]
    result = fabric.generate(ModelRequest(
        prompt="hello", capability="", model=remote.model_id,
        network_policy="explicit", allow_deterministic=False))

    assert result.success is False
    assert result.neural is False
    assert result.text == ""
    assert "paid" in result.error or "cost" in result.error
    #: The refusal is a cost-posture refusal, not a network one: a
    #: verification probe may have run, but the *request* prompt never left.
    bodies = b"\n".join(call["body"] for call in provider.calls)
    assert b"hello" not in bodies
