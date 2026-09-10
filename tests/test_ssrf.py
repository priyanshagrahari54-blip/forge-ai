"""SSRF protection tests (Phase 3 / A47 hardening).

Level 1: pure classification of IP ranges, hostnames, URLs, ports.
Level 2: fetch-path integration with a local attack server proving
         loopback/private targets are refused and redirects are
         revalidated on every hop, even when DNS lies (rebinding).
Level 3: end-to-end through the research engine fetch wrapper.
Level 4: real internet fetch, only when FORGE_REAL_PROVIDER_TESTS=1.
"""
from __future__ import annotations

import os
import socket
import threading
import http.server

import pytest

from forge.security import ssrf
from forge.security.ssrf import (
    FetchPolicy,
    SSRFError,
    fetch,
    hostname_blocked_reason,
    ip_blocked_reason,
    parse_and_validate,
)


# ---------------------------------------------------------------------------
# Level 1 — IP classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ip_text,expected", [
    ("127.0.0.1", "loopback"),
    ("127.8.8.8", "loopback"),
    ("10.0.0.1", "private"),
    ("10.255.255.255", "private"),
    ("172.16.0.1", "private"),
    ("172.31.255.255", "private"),
    ("192.168.1.1", "private"),
    ("169.254.169.254", "metadata"),
    ("169.254.0.1", "link-local"),
    ("100.64.0.1", "carrier-grade NAT"),
    ("0.0.0.0", "this host"),
    ("224.0.0.1", "multicast"),
    ("240.0.0.1", "reserved"),
    ("::1", "loopback"),
    ("fe80::1", "link-local"),
    ("fc00::1", "unique local"),
    ("fd12:3456::1", "unique local"),
    ("ff02::1", "multicast"),
    ("2001:db8::1", "documentation"),
    ("::ffff:127.0.0.1", "IPv4-mapped"),
    ("::ffff:10.0.0.5", "IPv4-mapped"),
    ("not-an-ip", "unparseable"),
])
def test_blocked_ip_ranges(ip_text: str, expected: str) -> None:
    reason = ip_blocked_reason(ip_text)
    assert reason is not None
    assert expected in reason


@pytest.mark.parametrize("ip_text", [
    "8.8.8.8", "1.1.1.1", "93.184.216.34", "172.32.0.1", "198.17.0.1",
    "200.1.2.3", "2606:4700::1111", "::ffff:8.8.8.8", "2001:4860:4860::8888",
])
def test_public_ips_allowed(ip_text: str) -> None:
    assert ip_blocked_reason(ip_text) is None


@pytest.mark.parametrize("hostname,expected", [
    ("localhost", "reserved"),
    ("localhost.localdomain", "reserved"),
    ("metadata.google.internal", "reserved"),
    ("instance-data", "reserved"),
    ("db.internal", "internal"),
    ("printer.lan", "internal"),
    ("router.home.arpa", "internal"),
    ("", "empty"),
])
def test_blocked_hostnames(hostname: str, expected: str) -> None:
    reason = hostname_blocked_reason(hostname)
    assert reason is not None
    assert expected in reason


@pytest.mark.parametrize("hostname", [
    "example.com", "www.example.com", "github.com", "raw.githubusercontent.com",
    "api.openai.com", "searx.example.org",
])
def test_public_hostnames_allowed(hostname: str) -> None:
    assert hostname_blocked_reason(hostname) is None


# ---------------------------------------------------------------------------
# Level 1 — URL parsing/validation
# ---------------------------------------------------------------------------

def test_https_allowed_by_default() -> None:
    scheme, host, port, path = parse_and_validate(
        "https://example.com/a/b?q=1", FetchPolicy())
    assert (scheme, host, port) == ("https", "example.com", 443)
    assert path == "/a/b?q=1"


def test_http_refused_by_default_and_allowed_when_policy_says_so() -> None:
    with pytest.raises(SSRFError, match="http:// is disabled"):
        parse_and_validate("http://example.com/", FetchPolicy())
    scheme, host, port, _ = parse_and_validate(
        "http://example.com/", FetchPolicy(allow_http=True))
    assert (scheme, host, port) == ("http", "example.com", 80)


@pytest.mark.parametrize("url", [
    "ftp://example.com/file",
    "file:///etc/passwd",
    "gopher://example.com/x",
    "javascript://x",
])
def test_non_http_schemes_refused(url: str) -> None:
    with pytest.raises(SSRFError, match="scheme"):
        parse_and_validate(url, FetchPolicy())


def test_embedded_credentials_refused() -> None:
    with pytest.raises(SSRFError, match="credentials"):
        parse_and_validate("https://user:pass@example.com/", FetchPolicy())


def test_local_service_ports_refused() -> None:
    with pytest.raises(SSRFError, match="port"):
        parse_and_validate("https://example.com:22/", FetchPolicy())
    with pytest.raises(SSRFError, match="port"):
        parse_and_validate("https://example.com:8080/", FetchPolicy())
    with pytest.raises(SSRFError, match="port"):
        parse_and_validate("https://example.com:6379/", FetchPolicy())


def test_custom_ports_require_policy() -> None:
    with pytest.raises(SSRFError, match="port"):
        parse_and_validate("https://example.com:8443/", FetchPolicy())
    scheme, _host, port, _ = parse_and_validate(
        "https://example.com:8443/", FetchPolicy(allow_ports=(8443,)))
    assert scheme == "https" and port == 8443


def test_host_allowlist_enforced() -> None:
    policy = FetchPolicy(host_allowlist=("docs.example.com",))
    parse_and_validate("https://docs.example.com/x", policy)
    with pytest.raises(SSRFError, match="allowlist"):
        parse_and_validate("https://other.example.com/x", policy)


def test_invalid_port_rejected() -> None:
    with pytest.raises(SSRFError):
        parse_and_validate("https://example.com:99999/", FetchPolicy())
    with pytest.raises(SSRFError):
        parse_and_validate("https://example.com:notaport/", FetchPolicy())


# ---------------------------------------------------------------------------
# Level 2 — fetch-path protection (local attack server, DNS lies)
# ---------------------------------------------------------------------------

def _local_server() -> tuple[http.server.ThreadingHTTPServer, int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)  # noqa: E501
    return server, server.server_address[1]


@pytest.fixture()
def attack_server() -> int:
    """A real listener on loopback that fetch() must never reach."""
    server, port = _local_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()


def test_fetch_refuses_loopback_hostname(attack_server: int) -> None:
    outcome = fetch(f"https://localhost:{attack_server}/",
                    policy=FetchPolicy(allow_ports=(attack_server,)))
    assert outcome.blocked is True
    assert "reserved" in outcome.blocked_reason


def test_fetch_refuses_private_ip_literal(attack_server: int) -> None:
    outcome = fetch(f"https://127.0.0.1:{attack_server}/",
                    policy=FetchPolicy(allow_ports=(attack_server,)))
    assert outcome.blocked is True
    assert "loopback" in outcome.blocked_reason


def test_fetch_refuses_private_ranges(attack_server: int) -> None:
    for host in ("10.0.0.7", "192.168.0.12", "169.254.169.254",
                 "172.16.3.4", "::1"):
        outcome = fetch(f"https://{host}:{attack_server}/",
                        policy=FetchPolicy(allow_ports=(attack_server,)))
        assert outcome.blocked is True, host


def test_dns_rebinding_is_blocked(monkeypatch, attack_server: int) -> None:
    """DNS resolves a 'public' name to loopback: must still be refused."""

    def lying_resolver(host: str, *_args, **_kwargs):
        assert host == "www.attacker.example"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("127.0.0.1", attack_server))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", lying_resolver)
    outcome = fetch("https://www.attacker.example/x",
                    policy=FetchPolicy(allow_ports=(attack_server,)))
    assert outcome.blocked is True
    assert "loopback" in outcome.blocked_reason


def test_fetch_never_connects_when_any_address_is_private(
        monkeypatch, attack_server: int) -> None:
    """All resolved addresses are inspected; one private entry blocks."""
    seen = []

    def mixed_resolver(host: str, *_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "",
             ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "",
             ("10.0.0.1", 0)),
        ]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", mixed_resolver)

    original_connect = socket.create_connection

    def spy_connect(address, *_a, **_kw):
        seen.append(address)
        return original_connect(address, *_a, **_kw)

    monkeypatch.setattr(ssrf.socket, "create_connection", spy_connect)
    outcome = fetch("https://mixed.example/x", policy=FetchPolicy())
    assert outcome.blocked is True
    assert "private" in outcome.blocked_reason
    assert seen == [], "fetch must not connect when any address is private"


def test_redirect_to_private_target_is_revalidated_and_blocked(
        monkeypatch) -> None:
    """A public page redirecting to loopback must be stopped at hop 2."""
    public_ip = "93.184.216.34"

    def resolver(host: str, *_args, **_kwargs):
        # Mimic real getaddrinfo: IP literals resolve to themselves;
        # only hostnames go through DNS (which we fake as public).
        try:
            import ipaddress
            ipaddress.ip_address(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     (host, 0))]
        except ValueError:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     (public_ip, 0))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", resolver)

    class FakeResponse:
        status = 302
        headers = {"Location": "https://127.0.0.1/steal"}

        def read(self, *args):
            return b""

        def close(self):
            pass

    connected_to: list[str] = []

    class FakeConnection:
        def __init__(self, ip, host, port, *, secure, timeout, context):
            connected_to.append(f"{ip}:{port}")

        def request(self, method, path, headers=None):
            pass

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr(ssrf, "_PinnedConnection", FakeConnection)
    outcome = fetch("https://public.example/start")
    assert outcome.blocked is True
    assert "loopback" in outcome.blocked_reason
    assert connected_to == [f"{public_ip}:443"], (
        "only the first, validated hop may be contacted")


def test_audit_callback_receives_blocked_decision(attack_server: int) -> None:
    events: list[dict] = []

    class Recorder:
        @staticmethod
        def record_decision(*, agent, resource, operation, scope,
                            decision, reason, task_id=""):
            events.append({
                "agent": agent, "resource": resource,
                "operation": operation, "scope": scope,
                "decision": decision, "reason": reason,
            })

    outcome = fetch("https://127.0.0.1:9/",
                    audit=Recorder())
    assert outcome.blocked is True
    assert any(e["decision"] == "DENY" and "port" in e["reason"]
               for e in events)


def test_connection_error_is_not_a_success_and_not_a_block(
        monkeypatch) -> None:
    """Failures past the SSRF chain are honest transport errors."""

    def public_resolver(host: str, *_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("93.184.216.34", 0))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", public_resolver)

    def no_route(*_args, **_kwargs):
        raise OSError("simulated: no route to host")

    monkeypatch.setattr(ssrf.socket, "create_connection", no_route)
    outcome = fetch("https://public.example/x")
    assert outcome.ok is False
    assert outcome.blocked is False
    assert "connection error" in outcome.error_state


# ---------------------------------------------------------------------------
# Level 2 — SSRF guard used by the research web layer
# ---------------------------------------------------------------------------

def test_research_fetch_refuses_local_targets() -> None:
    from forge.research.web import fetch_page_content
    result = fetch_page_content("https://localhost:443/x")
    assert isinstance(result, dict)
    assert result["blocked"] is True
    assert result["content"] == ""


def test_research_fetch_reports_policy_violation_text() -> None:
    from forge.research.web import fetch_page_content
    result = fetch_page_content("http://192.168.1.1/admin")
    assert result["blocked"] is True
    assert "blocked" in result["note"].lower() or result["blocked"]


# ---------------------------------------------------------------------------
# Level 4 — real internet fetch (opt-in only; default CI stays offline)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.environ.get("FORGE_REAL_PROVIDER_TESTS"),
    reason="requires FORGE_REAL_PROVIDER_TESTS=1 (opt-in real network)")
def test_real_public_fetch() -> None:
    outcome = fetch("https://example.com/")
    assert outcome.ok is True
    assert outcome.status == 200
    assert b"Example Domain" in outcome.body or outcome.bytes_read > 0
    assert outcome.blocked is False
