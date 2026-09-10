"""SSRF-hardened outbound HTTP(S) fetching (A47 research + general use).

Every URL that Forge fetches on behalf of a user or agent must pass
this module. The enforced chain is:

    URL
     -> scheme validation            (https by default; http only when policy allows)
     -> hostname validation          (blocked literal hostnames, DNS-suffix policy)
     -> DNS resolution               (ALL resolved addresses inspected)
     -> IP classification            (loopback / private / link-local / CGNAT /
                                      multicast / reserved / v4-mapped rechecked)
     -> port validation              (no arbitrary local ports by default)
     -> network policy               (explicit allowlist of hosts when configured)
     -> request                      (connection pinned to the validated address,
                                      TLS certificate still validated against the
                                      real hostname; no DNS-rebinding window)
     -> redirects                    (every hop revalidated through the same chain)
     -> bounded response             (size cap, content-type allowlist, no
                                      decompression bombs: identity encoding only)

Fail-closed: any violation raises :class:`SSRFError` before a byte is
sent, and redirect targets are revalidated before the next request.
The module never touches localhost, private ranges, link-local,
metadata endpoints, or internal hostnames.

Auditing: callers may pass an ``audit`` object exposing
``record_decision(*, agent, resource, operation, scope, decision,
reason, task_id)`` (the forge.security.audit.AuditLog shape); every
blocked hop, redirect, and successful fetch is recorded there.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.parse
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# classification tables
# ---------------------------------------------------------------------------

#: IPv4 ranges Forge refuses to fetch, with the reason shown to the caller.
_BLOCKED_IPV4: tuple[tuple[ipaddress.IPv4Network, str], ...] = (
    (ipaddress.ip_network("0.0.0.0/8"), "this host"),
    (ipaddress.ip_network("10.0.0.0/8"), "private network"),
    (ipaddress.ip_network("100.64.0.0/10"), "carrier-grade NAT"),
    (ipaddress.ip_network("127.0.0.0/8"), "loopback"),
    (ipaddress.ip_network("169.254.0.0/16"), "link-local / cloud metadata"),
    (ipaddress.ip_network("172.16.0.0/12"), "private network"),
    (ipaddress.ip_network("192.0.0.0/24"), "IETF protocol assignments"),
    (ipaddress.ip_network("192.0.2.0/24"), "documentation range"),
    (ipaddress.ip_network("192.168.0.0/16"), "private network"),
    (ipaddress.ip_network("198.18.0.0/15"), "benchmarking range"),
    (ipaddress.ip_network("198.51.100.0/24"), "documentation range"),
    (ipaddress.ip_network("203.0.113.0/24"), "documentation range"),
    (ipaddress.ip_network("224.0.0.0/4"), "multicast"),
    (ipaddress.ip_network("240.0.0.0/4"), "reserved"),
)

_BLOCKED_IPV6: tuple[tuple[ipaddress.IPv6Network, str], ...] = (
    (ipaddress.ip_network("::/128"), "unspecified"),
    (ipaddress.ip_network("::1/128"), "loopback"),
    (ipaddress.ip_network("::ffff:0:0/96"), "IPv4-mapped"),  # rechecked as IPv4
    (ipaddress.ip_network("64:ff9b::/96"), "IPv4-translated"),  # rechecked as IPv4
    (ipaddress.ip_network("100::/64"), "discard-only"),
    (ipaddress.ip_network("2001:db8::/32"), "documentation range"),
    (ipaddress.ip_network("fc00::/7"), "unique local"),
    (ipaddress.ip_network("fe80::/10"), "link-local"),
    (ipaddress.ip_network("ff00::/8"), "multicast"),
)

#: Literal hostnames that are always refused regardless of resolution.
_BLOCKED_HOSTNAMES = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "metadata", "metadata.google.internal", "instance-data",
    "instance-data.ec2.internal", "kubernetes.default", "dockerhost",
})

#: Hostname suffixes treated as internal infrastructure.
_INTERNAL_SUFFIXES = (
    ".internal", ".local", ".lan", ".home.arpa", ".corp", ".intranet",
    ".cloudapp.net", ".azurewebsites.net", ".servicebus.windows.net",
)

#: Default ports that are never allowed (well-known local services).
_BLOCKED_PORTS = frozenset({
    21, 22, 23, 25, 53, 111, 135, 137, 139, 445, 873, 1433, 1521,
    2049, 2375, 2376, 3000, 3306, 5432, 5900, 5984, 6379, 7001, 8000,
    8080, 8081, 8443, 9000, 9090, 9200, 9300, 11211, 27017,
})

#: Content types the fetcher will actually read (prefix match). Everything
#: else is refused before the body is downloaded.
_ALLOWED_CONTENT_PREFIXES = (
    "text/", "application/json", "application/xml", "application/xhtml",
    "application/javascript", "application/x-javascript",
)

DEFAULT_USER_AGENT = "ForgeBot/1.0 (+https://github.com/priyanshagrahari54-blip/forge-ai; research)"
DEFAULT_MAX_BYTES = 100_000
DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_REDIRECTS = 3


class SSRFError(Exception):
    """A URL was refused by the network policy before any request was sent."""

    def __init__(self, reason: str, *, url: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.url = url

    def to_dict(self) -> dict[str, Any]:
        return {"blocked": True, "reason": self.reason, "url": self.url}


@dataclass(frozen=True)
class FetchPolicy:
    """Network policy for one fetch operation. Fail-closed by default."""

    #: Permit plain http:// in addition to https://. Default: https only.
    allow_http: bool = False
    #: Optional exact-match host allowlist; empty = any public host allowed.
    host_allowlist: tuple[str, ...] = ()
    #: Hosts (exact) for which private/reserved IPs are tolerated. This is
    #: ONLY for operator-configured endpoints (e.g. a SearXNG instance on a
    #: LAN). User/agent supplied URLs never get this exemption.
    private_allowed_hosts: tuple[str, ...] = ()
    #: Optional ports allowed in addition to 80/443 (only when the scheme
    #: is also permitted). All other ports are refused.
    allow_ports: tuple[int, ...] = ()
    #: Hard cap on redirects followed.
    max_redirects: int = DEFAULT_MAX_REDIRECTS
    #: Per-hop timeout in seconds (DNS + connect + each read).
    timeout: float = DEFAULT_TIMEOUT
    #: Maximum response bytes read (body).
    max_bytes: int = DEFAULT_MAX_BYTES
    #: Content-type prefixes permitted; other types are refused.
    allowed_content_prefixes: tuple[str, ...] = _ALLOWED_CONTENT_PREFIXES
    user_agent: str = DEFAULT_USER_AGENT
    #: Method name used by audit callbacks.
    audit_operation: str = "url_fetch"
    audit_agent: str = "forge-network"


def ip_blocked_reason(ip_text: str) -> str | None:
    """Return the policy reason if ``ip_text`` is refused, else ``None``.

    Pure function over IP literals (IPv4, IPv6, and IPv4-mapped IPv6),
    so it is directly unit-testable without DNS or sockets.
    """
    try:
        address = ipaddress.ip_address(ip_text)
    except ValueError:
        return f"unparseable IP address {ip_text!r}"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        mapped = str(address.ipv4_mapped)
        inner = ip_blocked_reason(mapped)
        if inner:
            return f"IPv4-mapped address {mapped}: {inner}"
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.sixtofour:
        # 6to4 embeds an IPv4 address in 2002:xxxx/16.
        embedded = ipaddress.IPv4Address(
            int(address.sixtofour))
        inner = ip_blocked_reason(str(embedded))
        if inner:
            return f"6to4-embedded address {embedded}: {inner}"
    for network, reason in _BLOCKED_IPV6 if address.version == 6 \
            else _BLOCKED_IPV4:
        if address in network:
            return reason
    return None


#: Maps the classification-table reason strings onto machine-readable
#: destination classes (single vocabulary shared by every policy that
#: inspects resolved addresses — research fetch, SSH destinations,
#: operator proxy endpoints).
_BLOCK_REASON_TO_CLASS = {
    "this host": "this-host",
    "private network": "private",
    "carrier-grade NAT": "cg-nat",
    "loopback": "loopback",
    "link-local / cloud metadata": "link-local",
    "IETF protocol assignments": "reserved",
    "documentation range": "documentation",
    "benchmarking range": "benchmark",
    "multicast": "multicast",
    "reserved": "reserved",
    "unspecified": "unspecified",
    "IPv4-mapped": "ipv4-mapped",      # reclassified as the inner address
    "IPv4-translated": "ipv4-translated",  # reclassified as the inner address
    "discard-only": "reserved",
    "unique local": "private",
    "link-local": "link-local",
}

#: The cloud-metadata endpoint is refused *even under an operator
#: private-network exemption*: no legitimate SSH/execute/proxy target
#: is ever the host's own metadata service.
_CLOUD_METADATA_V4 = ipaddress.ip_network("169.254.169.254/32")


def ip_class(ip_text: str) -> str:
    """Classify one IP literal (no DNS) for destination policy.

    Returns a machine-readable class: ``public``, ``loopback``,
    ``private``, ``link-local``, ``cloud-metadata``, ``cg-nat``,
    ``this-host``, ``documentation``, ``benchmark``, ``multicast``,
    ``reserved``, ``unspecified``, or ``unparseable``. IPv4-mapped and
    6to4 IPv6 addresses are reclassified as their embedded IPv4
    address; the classifier shares its ranges with
    :func:`ip_blocked_reason`, so policy layers cannot drift apart.
    """
    try:
        address = ipaddress.ip_address(ip_text)
    except ValueError:
        return "unparseable"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return ip_class(str(address.ipv4_mapped))
    if isinstance(address, ipaddress.IPv6Address) and address.sixtofour:
        return ip_class(str(ipaddress.IPv4Address(int(address.sixtofour))))
    if address.version == 4 and address in _CLOUD_METADATA_V4:
        return "cloud-metadata"
    for network, reason in _BLOCKED_IPV6 if address.version == 6 \
            else _BLOCKED_IPV4:
        if address in network:
            return _BLOCK_REASON_TO_CLASS.get(reason, "reserved")
    return "public"


def hostname_blocked_reason(hostname: str) -> str | None:
    """Return the policy reason for a literal hostname, else ``None``."""
    host = (hostname or "").strip().lower().rstrip(".")
    if not host:
        return "empty hostname"
    if host in _BLOCKED_HOSTNAMES:
        return f"hostname {host!r} is reserved"
    for suffix in _INTERNAL_SUFFIXES:
        if host.endswith(suffix):
            return f"hostname {host!r} looks internal (suffix {suffix})"
    if host.isdigit():
        return f"bare numeric hostname {host!r}"
    return None


def parse_and_validate(url: str, policy: FetchPolicy) -> tuple[str, str, int, str]:
    """Validate scheme/hostname/port and return ``(scheme, host, port, path)``.

    Raises :class:`SSRFError` on any violation. DNS resolution happens in
    :func:`resolve_public_ips`; callers must use the returned address.
    """
    if not isinstance(url, str):
        raise SSRFError("url must be a string")
    url = url.strip()
    if len(url) > 2048:
        raise SSRFError("url exceeds the 2048-character limit")
    try:
        parsed = urllib.parse.urlparse(url)
        port = parsed.port  # may raise ValueError on malformed ports
    except ValueError:
        raise SSRFError("url has an invalid port", url=url) from None
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("https", "http"):
        raise SSRFError(f"scheme {scheme!r} is not permitted (https only by default)")
    if not policy.allow_http and scheme == "http":
        raise SSRFError("http:// is disabled by policy; use https://")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise SSRFError("url has no hostname")
    reason = hostname_blocked_reason(host)
    if reason:
        raise SSRFError(reason)
    if policy.host_allowlist and host not in policy.host_allowlist:
        raise SSRFError(
            f"host {host!r} is not on the network allowlist",
            url=url)
    if port is None:
        port = 443 if scheme == "https" else 80
    elif not (0 < port < 65536):
        raise SSRFError(f"port {port} out of range", url=url)
    else:
        default_port = 443 if scheme == "https" else 80
        if port != default_port:
            # An explicit policy allowlist entry overrides the blocked list
            # (operator opt-in); otherwise local-service ports and any
            # unlisted port are refused.
            if port in policy.allow_ports:
                pass
            elif port in _BLOCKED_PORTS:
                raise SSRFError(
                    f"port {port} is a blocked local-service port", url=url)
            else:
                raise SSRFError(
                    f"port {port} is not permitted by network policy",
                    url=url)
    # Reject any credentials embedded in the URL (avoid secret leakage into logs
    # and Host/Proxy headers) and any userinfo at all.
    if parsed.username is not None or parsed.password is not None:
        raise SSRFError("urls with embedded credentials are refused", url=url)
    if parsed.fragment:
        # Fragments are not sent to the server; drop silently is fine, but we
        # refuse for strictness? They are harmless — strip below.
        pass
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return scheme, host, port, path


def resolve_public_ips(host: str, policy: FetchPolicy | None = None
                       ) -> tuple[str, str]:
    """Resolve ``host`` and return ``(ip_text, ip_family_label)``.

    Every address the resolver returns must be public; if any one is
    blocked the whole host is refused (fail-closed, no per-IP picking).
    ``policy.private_allowed_hosts`` may exempt an operator-configured
    host (never user input).
    """
    policy = policy or FetchPolicy()
    try:
        infos = socket.getaddrinfo(
            host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SSRFError(f"DNS resolution failed for {host!r}: {exc}", url=host) from exc
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise SSRFError(f"DNS returned no addresses for {host!r}", url=host)
    exempt = host in policy.private_allowed_hosts
    for address in addresses:
        reason = None if exempt else ip_blocked_reason(address)
        if reason:
            raise SSRFError(
                f"host {host!r} resolves to {address} ({reason}) — refused", url=host)
    family = "ipv6" if ":" in addresses[0] else "ipv4"
    return addresses[0], family


class _PinnedConnection(http.client.HTTPConnection):
    """HTTP(S) connection pinned to a pre-validated IP address.

    TLS still validates the certificate against the *hostname* the caller
    asked for (``server_hostname=host``), so pinning the TCP connection to
    the validated IP does not weaken certificate checks and closes the
    classic DNS-rebinding window between validation and connect.
    """

    def __init__(self, ip: str, host: str, port: int, *,
                 secure: bool, timeout: float, context: ssl.SSLContext | None):
        self._pinned_ip = ip
        self._secure = secure
        self._ssl_context = context
        super().__init__(host, port, timeout=timeout)

    def connect(self) -> None:  # noqa: D102
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), timeout=self.timeout)
        if self._secure:
            self.sock = self._ssl_context.wrap_socket(
                self.sock, server_hostname=self.host)


@dataclass
class FetchOutcome:
    """Machine-readable outcome of one bounded fetch."""

    ok: bool
    status: int = 0
    final_url: str = ""
    content_type: str = ""
    body: bytes = b""
    bytes_read: int = 0
    redirects: int = 0
    error_state: str = ""      # SSRFError-reason or HTTP-level failure text
    blocked: bool = False
    blocked_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "final_url": self.final_url,
            "content_type": self.content_type,
            "bytes_read": self.bytes_read,
            "redirects": self.redirects,
            "blocked": self.blocked,
            "error_state": self.error_state or self.blocked_reason,
        }


def _audit(audit: Any, agent: str, operation: str, scope: str,
           allowed: bool, reason: str) -> None:
    """Best-effort audit logging through the Forge AuditLog interface."""
    if audit is None:
        return
    try:
        record = getattr(audit, "record_decision", None)
        if record is None:
            cb = audit if callable(audit) else None
            if cb:
                cb({"agent": agent, "operation": operation,
                    "scope": scope, "allowed": allowed, "reason": reason})
            return
        record(agent=agent, resource="network", operation=operation,
               scope=scope,
               decision="ALLOW" if allowed else "DENY", reason=reason)
    except Exception:
        pass


def fetch(url: str, *, policy: FetchPolicy | None = None,
          audit: Any = None) -> FetchOutcome:
    """Fetch ``url`` under the network policy with full SSRF protection.

    Each redirect hop goes through the same scheme/hostname/DNS/IP/port
    validation chain before its request is issued. Response bodies are
    bounded by ``policy.max_bytes``, content types are allowlisted before
    the body is read, and only identity transfer encoding is accepted
    (no decompression bombs).
    """
    policy = policy or FetchPolicy()
    outcome = FetchOutcome(ok=False, final_url=url)
    current_url = url.strip()
    scheme = ""
    host = ""
    port = 443
    path = "/"
    redirects = 0

    while True:
        # 1. scheme -> hostname -> port validation
        try:
            scheme, host, port, path = parse_and_validate(
                current_url, policy)
        except SSRFError as exc:
            outcome.blocked = True
            outcome.blocked_reason = exc.reason
            _audit(audit, policy.audit_agent, policy.audit_operation,
                   scope=current_url[:200], allowed=False, reason=exc.reason)
            return outcome

        # 2. DNS resolution; every address must be public.
        try:
            ip_text, _family = resolve_public_ips(host, policy)
        except SSRFError as exc:
            outcome.blocked = True
            outcome.blocked_reason = exc.reason
            _audit(audit, policy.audit_agent, policy.audit_operation,
                   scope=current_url[:200], allowed=False, reason=exc.reason)
            return outcome

        _audit(audit, policy.audit_agent, policy.audit_operation,
               scope=f"{current_url[:200]} -> {ip_text}", allowed=True,
               reason="ssrf chain passed")

        # 3. bounded request against the pinned address.
        secure = scheme == "https"
        context = ssl.create_default_context() if secure else None
        try:
            conn = _PinnedConnection(
                ip_text, host, port, secure=secure,
                timeout=policy.timeout, context=context)
            headers = {
                "Host": host if port in (80, 443) else f"{host}:{port}",
                "User-Agent": policy.user_agent,
                "Accept-Encoding": "identity",   # no decompression bombs
                "Connection": "close",
            }
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            status = response.status
            content_type = (response.headers.get("Content-Type", "") or "")
            content_type = content_type.split(";")[0].strip().lower()

            location = ""
            if status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location", "") or ""
            length_header = response.headers.get("Content-Length")
            if length_header is not None:
                try:
                    if int(length_header) > policy.max_bytes:
                        # refuse early; do not download an oversized body
                        outcome.ok = False
                        outcome.status = status
                        outcome.content_type = content_type
                        outcome.redirects = redirects
                        outcome.error_state = (
                            f"response body exceeds the {policy.max_bytes}-byte limit")
                        _audit(audit, policy.audit_agent,
                               policy.audit_operation, scope=current_url[:200],
                               allowed=False, reason=outcome.error_state)
                        conn.close()
                        return outcome
                except ValueError:
                    pass
            encoding = (response.headers.get("Content-Encoding", "") or "").lower()
            if encoding and encoding != "identity":
                outcome.ok = False
                outcome.status = status
                outcome.content_type = content_type
                outcome.redirects = redirects
                outcome.error_state = (
                    f"compressed transfer ({encoding}) is refused; "
                    "re-request with identity encoding only")
                _audit(audit, policy.audit_agent, policy.audit_operation,
                       scope=current_url[:200], allowed=False,
                       reason=outcome.error_state)
                conn.close()
                return outcome

            if status in (301, 302, 303, 307, 308):
                conn.close()
                if redirects >= policy.max_redirects:
                    outcome.blocked = True
                    outcome.blocked_reason = (
                        f"redirect limit ({policy.max_redirects}) exceeded")
                    _audit(audit, policy.audit_agent, policy.audit_operation,
                           scope=current_url[:200], allowed=False,
                           reason=outcome.blocked_reason)
                    return outcome
                if not location:
                    outcome.ok = False
                    outcome.status = status
                    outcome.redirects = redirects
                    outcome.error_state = "redirect without Location header"
                    return outcome
                redirects += 1
                # 4. revalidate the redirect target through the same chain
                current_url = urllib.parse.urljoin(current_url, location.strip())
                _audit(audit, policy.audit_agent, policy.audit_operation,
                       scope=f"redirect #{redirects} to {current_url[:200]}",
                       allowed=True, reason="revalidating redirect target")
                continue

            # 5. content-type allowlist. An explicit non-allowed type is
            # refused before the body is read. A *missing* type is probed:
            # read a bounded prefix and refuse if it looks binary, so
            # text servers that omit the header still work.
            allowed_type = content_type.startswith(
                policy.allowed_content_prefixes)
            if not allowed_type:
                probe = response.read(1024)
                looks_binary = content_type != "" or b"\x00" in probe
                if looks_binary:
                    outcome.ok = False
                    outcome.status = status
                    outcome.content_type = content_type
                    outcome.redirects = redirects
                    outcome.error_state = (
                        f"content-type {content_type!r} is not on the "
                        "allowlist")
                    _audit(audit, policy.audit_agent,
                           policy.audit_operation, scope=current_url[:200],
                           allowed=False, reason=outcome.error_state)
                    conn.close()
                    return outcome
                body = probe
                while len(body) <= policy.max_bytes:
                    chunk = response.read(
                        min(65536, policy.max_bytes - len(body) + 1))
                    if not chunk:
                        break
                    body += chunk
                if len(body) > policy.max_bytes:
                    conn.close()
                    outcome.ok = False
                    outcome.status = status
                    outcome.content_type = content_type
                    outcome.redirects = redirects
                    outcome.error_state = (
                        f"response body exceeds the "
                        f"{policy.max_bytes}-byte limit")
                    _audit(audit, policy.audit_agent,
                           policy.audit_operation, scope=current_url[:200],
                           allowed=False, reason=outcome.error_state)
                    return outcome
                conn.close()
                outcome.ok = status < 400
                outcome.status = status
                outcome.final_url = current_url
                outcome.content_type = content_type
                outcome.body = body
                outcome.bytes_read = len(body)
                outcome.redirects = redirects
                _audit(audit, policy.audit_agent, policy.audit_operation,
                       scope=current_url[:200], allowed=outcome.ok,
                       reason=f"http {status}, {len(body)} bytes")
                return outcome

            body = b""
            while True:
                chunk = response.read(min(65536, policy.max_bytes - len(body) + 1))
                if not chunk:
                    break
                body += chunk
                if len(body) > policy.max_bytes:
                    conn.close()
                    outcome.ok = False
                    outcome.status = status
                    outcome.content_type = content_type
                    outcome.redirects = redirects
                    outcome.error_state = (
                        f"response body exceeds the {policy.max_bytes}-byte limit")
                    _audit(audit, policy.audit_agent, policy.audit_operation,
                           scope=current_url[:200], allowed=False,
                           reason=outcome.error_state)
                    return outcome
            conn.close()
            outcome.ok = status < 400
            outcome.status = status
            outcome.final_url = current_url
            outcome.content_type = content_type
            outcome.body = body
            outcome.bytes_read = len(body)
            outcome.redirects = redirects
            _audit(audit, policy.audit_agent, policy.audit_operation,
                   scope=current_url[:200], allowed=outcome.ok,
                   reason=f"http {status}, {len(body)} bytes")
            return outcome
        except socket.timeout:
            outcome.ok = False
            outcome.status = 0
            outcome.redirects = redirects
            outcome.error_state = f"request timed out after {policy.timeout}s"
            return outcome
        except ssl.SSLError as exc:
            outcome.ok = False
            outcome.redirects = redirects
            outcome.error_state = f"TLS error: {exc}"
            return outcome
        except http.client.HTTPException as exc:
            outcome.ok = False
            outcome.redirects = redirects
            outcome.error_state = f"HTTP error: {exc}"
            return outcome
        except OSError as exc:
            outcome.ok = False
            outcome.redirects = redirects
            outcome.error_state = f"connection error: {exc}"
            return outcome


#: Convenience policy used by the research engine for page extraction.
RESEARCH_POLICY = FetchPolicy(
    allow_http=False,
    max_bytes=DEFAULT_MAX_BYTES,
    timeout=DEFAULT_TIMEOUT,
    max_redirects=DEFAULT_MAX_REDIRECTS,
)
