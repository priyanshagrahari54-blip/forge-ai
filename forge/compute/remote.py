"""Remote compute backends for A48.

Provides remote Python execution via:

- **local** — default, real subprocess (see ``forge.compute.engine``)
- **Colab-compatible kernel proxy** (``FORGE_COLAB_URL``) — an
  operator-provided kernel-proxy endpoint speaking the Colab JSON
  protocol (``POST /execute``). This is NOT a bundled Google Colab
  integration and the code makes no Google-account claim; a proxy URL
  configured by the operator is expected.
- **SSH** — strict, fail-closed remote host (``FORGE_COMPUTE_SSH_*``)
- **Modal** — serverless sandbox (``MODAL_TOKEN_ID`` + secret)

Security invariants for the remote backends:

- User code NEVER appears in a command line and never goes through a
  shell. It is delivered exclusively over stdin (SSH) or a request
  body (kernel proxy) to a fixed remote command: ``python3 -I -``
  (isolated mode, program read from stdin). No ``shell=True``
  anywhere.
- Host-key verification is mandatory for SSH:
  ``StrictHostKeyChecking=yes`` against the configured known_hosts
  file; password prompts are disabled (``BatchMode=yes``). There is
  no ``StrictHostKeyChecking=no`` anywhere.
- The destination must appear in an explicit allowlist
  (``FORGE_COMPUTE_SSH_ALLOWLIST``) — fail-closed when absent.
- Destination IP policy: before any connection, the configured
  hostname is resolved and **every** returned address is classified
  (public / loopback / private / link-local / cloud-metadata /
  multicast / reserved / …). If any address is non-public the
  destination is refused, unless the operator has explicitly opted
  into a private-network destination (``FORGE_COMPUTE_SSH_ALLOW_PRIVATE=1``
  / ``FORGE_COLAB_ALLOW_LOCALHOST=1`` / ``FORGE_COLAB_ALLOW_PRIVATE=1``
  — see below). Unresolvable/unknown hostnames fail closed. The
  cloud-metadata endpoint (``169.254.169.254``) is always refused.
- Every remote URL (including ``FORGE_COLAB_URL``) goes through
  parse -> normalize -> policy (scheme/credentials/port/name) ->
  DNS/IP safety -> connect, with no bypasses.
- User, host, and port are explicit; the hostname matches a strict
  pattern; connection, execution, and output are bounded; the whole
  remote process group is cancellable via timeout.
- Execution is policy-gated by the caller (control plane
  ``compute_execute`` -> TERMINAL policy, A33 approval flow), audited,
  and labeled with the backend name and honest status.
"""
from __future__ import annotations

import ipaddress
import os
import re
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

from forge.security.ssrf import ip_class

MAX_CODE = 6000
MAX_OUTPUT = 4000
REQUEST_TIMEOUT = 60.0

_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,252})$")
_USER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_ALLOWLIST_ENTRY_RE = re.compile(
    r"^(?P<user>[A-Za-z0-9._-]{1,64})@(?P<host>[A-Za-z0-9](?:[A-Za-z0-9._-]{0,252}))$|"  # noqa: E501
    r"^(?P<host_only>[A-Za-z0-9](?:[A-Za-z0-9._-]{0,252}))$")
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9_./~-]{1,500}$")
_KNOWN_HOSTS_RE = re.compile(r"^[A-Za-z0-9_./~-]{1,500}$")


class RemoteComputeError(RuntimeError):
    """Configuration/transport failure for a remote backend."""


# ---------------------------------------------------------------------------
# Destination-IP policy (A48 security hardening)
#
# Every backend hostname is resolved and *every* returned address is
# classified before any connection may be attempted. Non-public
# destinations are refused unless the operator has explicitly opted
# in (these env variables are operator-controlled; a hostname in the
# SSH allowlist alone never bypasses this policy):
#
#   FORGE_COMPUTE_SSH_ALLOW_PRIVATE=1   SSH destination may be any
#                                       non-public class (except the
#                                       never-allowed set below).
#   FORGE_COLAB_ALLOW_LOCALHOST=1       kernel proxy may point at a
#                                       loopback/link-local endpoint.
#   FORGE_COLAB_ALLOW_PRIVATE=1         kernel proxy may point at a
#                                       private-network endpoint.
#
# Cloud-metadata endpoints (169.254.169.254), multicast,
# documentation/benchmark ranges and other never-routes are refused
# even with an opt-in flag.
# ---------------------------------------------------------------------------

_SSH_PRIVATE_EXCEPTION_ENV = "FORGE_COMPUTE_SSH_ALLOW_PRIVATE"
_COLAB_LOCAL_EXCEPTION_ENV = "FORGE_COLAB_ALLOW_LOCALHOST"
_COLAB_PRIVATE_EXCEPTION_ENV = "FORGE_COLAB_ALLOW_PRIVATE"

#: Classes tolerated by the "local endpoint" opt-in.
_LOCAL_CLASSES = frozenset({"loopback", "link-local", "this-host",
                            "unspecified"})
#: Classes tolerated by the "private network" opt-in.
_PRIVATE_CLASSES = frozenset({"private", "cg-nat"})
#: Classes that are never permitted destinations, even with opt-ins.
_NEVER_DESTINATION_CLASSES = frozenset({
    "cloud-metadata", "multicast", "documentation", "benchmark",
    "reserved", "unparseable", "ipv4-mapped", "ipv4-translated"})


def _host_resolver(host: str) -> list[tuple[Any, ...]]:
    """Resolve helper used by the destination policy (patchable in tests).

    Returns the raw ``socket.getaddrinfo`` result list.
    """
    return socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)


def classify_destination(host: str) -> tuple[list[dict[str, str]], str]:
    """Resolve ``host`` and classify EVERY returned address.

    Returns ``(entries, error)`` where each entry is
    ``{"address": ip_text, "class": ip_class}`` (sorted, de-duplicated)
    and ``error`` is ``""`` on success. IP literals are classified
    directly (no DNS round trip, offline-deterministic). Fail-closed:
    unknown/unresolvable hostnames yield an error message, never an
    empty "allowed" list.
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return [], "empty hostname"
    try:
        address = ipaddress.ip_address(host)
        ip_texts = [str(address)]
    except ValueError:
        try:
            infos = _host_resolver(host)
        except (socket.gaierror, OSError) as exc:
            return [], f"could not be resolved ({exc})"
        ip_texts = sorted({info[4][0] for info in infos})
        if not ip_texts:
            return [], "DNS returned no addresses"
    entries = [{"address": ip_text, "class": ip_class(ip_text)}
               for ip_text in ip_texts]
    return entries, ""


def _exception_env_set(name: str) -> bool:
    return os.environ.get(name, "").strip() == "1"


def _refusal_message(purpose: str, host: str,
                     *, non_public: list[dict[str, str]],
                     opt_in_allowed: bool,
                     never: list[dict[str, str]],
                     hint: str) -> str:
    names = ", ".join(
        f"{entry['address']} ({entry['class']})" for entry in non_public)
    msg = (f"{purpose} destination {host!r} resolves to non-public "
           f"address(es): {names}; refused by destination-IP policy.")
    if never:
        never_names = ", ".join(
            f"{entry['address']} ({entry['class']})" for entry in never)
        msg += (f" {never_names} is in the never-routed set and is refused "
                "even with an operator exception.")
    elif not opt_in_allowed:
        msg += f" {hint}"
    return msg


def ssh_destination_policy(config: "SSHConfig") -> list[dict[str, str]]:
    """Destination-IP policy gate for the SSH backend.

    Resolves ``config.host`` and classifies every address. Raises
    :class:`RemoteComputeError` when the host is unresolvable (fail
    closed) or when any resolved address is non-public without the
    explicit operator opt-in ``FORGE_COMPUTE_SSH_ALLOW_PRIVATE=1``.
    Never-routed classes (cloud metadata, multicast, documentation,
    benchmark, reserved) are refused even with the opt-in. Returns the
    classified entries on success (public-only or opt-in'd).
    """
    entries, error = classify_destination(config.host)
    if error:
        raise RemoteComputeError(
            f"SSH destination {config.host!r} {error}; refusing to "
            "connect (fail-closed: unknown/unresolvable destinations "
            "are never allowed)")
    non_public = [e for e in entries if e["class"] != "public"]
    if not non_public:
        return entries
    never = [e for e in non_public
             if e["class"] in _NEVER_DESTINATION_CLASSES]
    opt_in = _exception_env_set(_SSH_PRIVATE_EXCEPTION_ENV)
    if opt_in and not never:
        return entries
    raise RemoteComputeError(_refusal_message(
        "SSH", config.host, non_public=non_public,
        opt_in_allowed=opt_in, never=never,
        hint=(f"if this is an explicit operator-controlled "
              f"private-network destination, set "
              f"{_SSH_PRIVATE_EXCEPTION_ENV}=1 (the host must still "
              "be on FORGE_COMPUTE_SSH_ALLOWLIST)")))


def colab_url_policy(url: str) -> str:
    """parse -> normalize -> policy -> DNS/IP safety for a proxy URL.

    Returns the normalized URL when the endpoint passes every check;
    raises :class:`RemoteComputeError` otherwise (before any byte is
    sent). Port and resolved-address policies are described in the
    module header; this function is pure policy and never connects.
    """
    from urllib.parse import urlparse

    url = (url or "").strip()
    if not url:
        raise RemoteComputeError("FORGE_COLAB_URL is empty")
    if len(url) > 2048:
        raise RemoteComputeError("FORGE_COLAB_URL is too long")
    if "://" not in url:
        url = f"https://{url}"
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise RemoteComputeError(
            f"FORGE_COLAB_URL could not be parsed: {exc}") from exc
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if scheme not in ("https", "http"):
        raise RemoteComputeError(
            "FORGE_COLAB_URL must be https:// (or http:// with "
            "FORGE_COLAB_ALLOW_HTTP=1)")
    if scheme == "http" and not _exception_env_set("FORGE_COLAB_ALLOW_HTTP"):
        raise RemoteComputeError(
            "FORGE_COLAB_URL uses http://; set FORGE_COLAB_ALLOW_HTTP=1 "
            "to permit it explicitly")
    if parsed.username is not None or parsed.password is not None:
        raise RemoteComputeError(
            "FORGE_COLAB_URL must not embed credentials")
    if not host:
        raise RemoteComputeError("FORGE_COLAB_URL has no hostname")
    # Fast literal-name policy (also caught by resolution below, kept
    # for deterministic offline refusal of obvious local endpoints).
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(
            (".local", ".internal")):
        if not _exception_env_set(_COLAB_LOCAL_EXCEPTION_ENV):
            raise RemoteComputeError(
                "FORGE_COLAB_URL points at a local endpoint; set "
                "FORGE_COLAB_ALLOW_LOCALHOST=1 to permit it explicitly")
    # Port policy: default ports only, unless the operator has opted
    # into a non-standard deployment (local or private endpoint flags).
    default_port = 443 if scheme == "https" else 80
    if parsed.port is not None and parsed.port != default_port:
        if not (_exception_env_set(_COLAB_LOCAL_EXCEPTION_ENV)
                or _exception_env_set(_COLAB_PRIVATE_EXCEPTION_ENV)):
            raise RemoteComputeError(
                f"FORGE_COLAB_URL uses non-default port {parsed.port}; "
                "set FORGE_COLAB_ALLOW_LOCALHOST=1 or "
                "FORGE_COLAB_ALLOW_PRIVATE=1 to permit it explicitly")
        if not (0 < parsed.port < 65536):
            raise RemoteComputeError(
                f"FORGE_COLAB_URL has an invalid port {parsed.port}")
    # Resolved-IP policy: every returned address must be public unless
    # the matching operator opt-in is set.
    entries, error = classify_destination(host)
    if error:
        raise RemoteComputeError(
            f"FORGE_COLAB_URL host {host!r} {error}; refusing "
            "(fail-closed: unresolvable endpoints are never allowed)")
    non_public = [e for e in entries if e["class"] != "public"]
    if non_public:
        local = [e for e in non_public if e["class"] in _LOCAL_CLASSES]
        private = [e for e in non_public
                   if e["class"] in _PRIVATE_CLASSES]
        never = [e for e in non_public
                 if e["class"] in _NEVER_DESTINATION_CLASSES]
        allow_local = _exception_env_set(_COLAB_LOCAL_EXCEPTION_ENV)
        allow_private = _exception_env_set(_COLAB_PRIVATE_EXCEPTION_ENV)
        permitted = (not never
                     and (not local or allow_local)
                     and (not private or allow_private))
        if not permitted:
            hints = []
            if local and not allow_local:
                hints.append(f"set {_COLAB_LOCAL_EXCEPTION_ENV}=1 for "
                             "loopback/link-local endpoints")
            if private and not allow_private:
                hints.append(f"set {_COLAB_PRIVATE_EXCEPTION_ENV}=1 for "
                             "private-network endpoints")
            raise RemoteComputeError(_refusal_message(
                "FORGE_COLAB_URL", host, non_public=non_public,
                opt_in_allowed=bool(allow_local or allow_private),
                never=never,
                hint="; ".join(hints) + "."))
    return url.rstrip("/")


@dataclass(frozen=True)
class SSHConfig:
    """Fail-closed SSH destination parsed from environment configuration."""

    user: str
    host: str
    port: int = 22
    allowlist: tuple[str, ...] = ()
    identity_file: str = ""
    known_hosts: str = ""
    connect_timeout: float = 10.0

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"

    def allowed(self) -> bool:
        """True only when the destination is explicitly allowlisted."""
        if not self.allowlist:
            return False
        return self.host in self.allowlist or \
            self.target in self.allowlist


def parse_ssh_config() -> SSHConfig:
    """Parse and validate SSH configuration from the environment.

    Fail-closed: missing allowlist, user, or malformed values raise
    :class:`RemoteComputeError` instead of degrading security.
    """
    spec = os.environ.get("FORGE_COMPUTE_SSH_HOST", "").strip()
    user_env = os.environ.get("FORGE_COMPUTE_SSH_USER", "").strip()
    port_env = os.environ.get("FORGE_COMPUTE_SSH_PORT", "").strip()
    allowlist_raw = os.environ.get("FORGE_COMPUTE_SSH_ALLOWLIST", "").strip()
    identity = os.environ.get("FORGE_COMPUTE_SSH_IDENTITY", "").strip()
    known_hosts = os.environ.get(
        "FORGE_COMPUTE_SSH_KNOWN_HOSTS",
        os.path.join(os.path.expanduser("~"), ".ssh", "known_hosts")).strip()

    user = ""
    host = ""
    port = 22

    if spec:
        # Accept "user@host", "host", "user@host:port", "host:port".
        if "@" in spec:
            user_part, _, host_part = spec.rpartition("@")
            user = user_part
        else:
            host_part = spec
        if ":" in host_part:
            host_text, _, port_text = host_part.rpartition(":")
            if not port_text.isdigit():
                raise RemoteComputeError(
                    "FORGE_COMPUTE_SSH_HOST has a non-numeric port")
            port = int(port_text)
            host = host_text
        else:
            host = host_part
    user = user or user_env
    if port_env:
        if not port_env.isdigit():
            raise RemoteComputeError(
                "FORGE_COMPUTE_SSH_PORT must be a number")
        port = int(port_env)
    if not user:
        raise RemoteComputeError(
            "SSH compute requires an explicit user "
            "(FORGE_COMPUTE_SSH_USER or user@host in "
            "FORGE_COMPUTE_SSH_HOST)")
    if not _USER_RE.match(user):
        raise RemoteComputeError(
            f"FORGE_COMPUTE_SSH_HOST contains an invalid user {user!r}")
    if not host or not _HOST_RE.match(host):
        raise RemoteComputeError(
            f"FORGE_COMPUTE_SSH_HOST contains an invalid hostname {host!r}")
    if not (0 < port < 65536):
        raise RemoteComputeError(f"invalid SSH port {port}")
    if not allowlist_raw:
        raise RemoteComputeError(
            "SSH compute is refused: FORGE_COMPUTE_SSH_ALLOWLIST must "
            "explicitly list the destination (fail-closed)")
    allowlist = tuple(
        entry.strip() for entry in allowlist_raw.split(",") if entry.strip())
    for entry in allowlist:
        if not _ALLOWLIST_ENTRY_RE.match(entry):
            raise RemoteComputeError(
                f"invalid allowlist entry {entry!r}")
    if identity and not _IDENTITY_RE.match(identity):
        raise RemoteComputeError("invalid SSH identity path")
    if known_hosts and not _KNOWN_HOSTS_RE.match(known_hosts):
        raise RemoteComputeError("invalid known_hosts path")
    return SSHConfig(user=user, host=host, port=port,
                     allowlist=allowlist,
                     identity_file=identity, known_hosts=known_hosts)


def _ssh_argv(config: SSHConfig) -> list[str]:
    """Build the ssh argv; user code never appears in this list.

    Only fixed options plus the validated destination are present. The
    remote command is constant: ``python3 -I -`` reads the program
    from stdin.
    """
    if not config.allowed():
        raise RemoteComputeError(
            f"destination {config.target} is not on the "
            "FORGE_COMPUTE_SSH_ALLOWLIST")
    argv = [
        "ssh",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={config.known_hosts}",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={int(config.connect_timeout)}",
        "-o", "LogLevel=ERROR",
        "-o", "IdentitiesOnly=yes",
    ]
    if config.port != 22:
        argv += ["-p", str(config.port)]
    if config.identity_file:
        argv += ["-i", config.identity_file]
    argv += [config.target, "python3", "-I", "-"]
    return argv


class _PipeReader(threading.Thread):
    """Daemon reader that drains a pipe, keeping only the first ``limit``
    bytes; anything beyond the cap is discarded so the child process can
    always finish writing while memory stays bounded."""

    def __init__(self, stream: Any, limit: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self._chunks: list[bytes] = []
        self._total = 0
        self.truncated = False
        self.done = threading.Event()

    def run(self) -> None:
        try:
            while True:
                chunk = os.read(self._stream.fileno(), 65536)
                if not chunk:
                    break
                if self._total < self._limit:
                    room = self._limit - self._total
                    self._chunks.append(chunk[:room])
                    self._total += len(chunk[:room])
                    if len(chunk) > room:
                        self.truncated = True
                else:
                    self.truncated = True
        except (OSError, ValueError):
            pass
        finally:
            try:
                self._stream.close()
            except OSError:
                pass
            self.done.set()

    def data(self) -> bytes:
        return b"".join(self._chunks)


def _execute_ssh_secure(code: str, config: SSHConfig,
                        timeout: float,
                        env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run ``code`` on the remote host over a pinned stdin transport.

    User code travels exclusively on stdin to the fixed remote command
    ``python3 -I -``; stdout/stderr are drained by bounded daemon
    readers (capped at ``MAX_OUTPUT`` each, overflow discarded), the
    whole remote process group is cancelled on timeout, and the result
    is labeled with the honest status.
    """
    argv = _ssh_argv(config)
    started = time.monotonic()
    hard_deadline = started + max(1.0, timeout) + 5.0
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True, env=env)
    except OSError as exc:
        return {"status": "failed",
                "output": f"SSH transport could not start: {exc}",
                "output_truncated": False,
                "elapsed_ms": 0.0, "backend": "ssh-remote",
                "timed_out": False}

    code_bytes = code.encode("utf-8")
    try:
        proc.stdin.write(code_bytes)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass  # remote exited before reading; fall through to wait()

    out_reader = _PipeReader(proc.stdout, MAX_OUTPUT)
    err_reader = _PipeReader(proc.stderr, MAX_OUTPUT)
    out_reader.start()
    err_reader.start()

    timed_out = False
    try:
        proc.wait(timeout=max(0.0, hard_deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True
        # Cancel the entire remote process group, not just the client.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                pass

    out_reader.done.wait(timeout=1.0)
    err_reader.done.wait(timeout=1.0)

    if timed_out:
        elapsed = time.monotonic() - started
        return {"status": "timeout",
                "output": f"SSH execution timed out after {timeout}s",
                "output_truncated": False,
                "elapsed_ms": round(elapsed * 1000, 1),
                "backend": "ssh-remote", "timed_out": True}

    output = (out_reader.data().decode("utf-8", errors="replace") +
              err_reader.data().decode("utf-8", errors="replace"))
    truncated = out_reader.truncated or err_reader.truncated
    output = output[:MAX_OUTPUT]
    elapsed = time.monotonic() - started
    return {
        "status": "succeeded" if proc.returncode == 0 else "failed",
        "output": output,
        "output_truncated": truncated,
        "elapsed_ms": round(elapsed * 1000, 1),
        "backend": "ssh-remote",
        "timed_out": False,
        "returncode": proc.returncode,
    }


class RemoteComputeBackend:
    """Execute Python code on a configured remote backend.

    Returns ``available()`` only when at least one backend is
    configured. All backends fail closed and label results honestly.
    """

    def __init__(self) -> None:
        self._colab_url = os.environ.get("FORGE_COLAB_URL", "").strip()
        self._modal_token = os.environ.get("MODAL_TOKEN_ID", "").strip()
        self._modal_secret = os.environ.get(
            "MODAL_TOKEN_SECRET", "").strip()
        self._ssh_config: SSHConfig | None = None
        if os.environ.get("FORGE_COMPUTE_SSH_HOST", "").strip() or \
                os.environ.get("FORGE_COMPUTE_SSH_USER", "").strip():
            try:
                self._ssh_config = parse_ssh_config()
            except RemoteComputeError:
                self._ssh_config = None  # surfaced honestly per call

    @property
    def ssh_config_error(self) -> str:
        """Reason the SSH backend is unavailable, or '' when configured."""
        if not (os.environ.get("FORGE_COMPUTE_SSH_HOST", "").strip()
                or os.environ.get("FORGE_COMPUTE_SSH_USER", "").strip()):
            return "No FORGE_COMPUTE_SSH_HOST configured."
        if self._ssh_config is None:
            try:
                parse_ssh_config()
                return ""
            except RemoteComputeError as exc:
                return str(exc)
        return ""

    @property
    def backend_name(self) -> str:
        if self._colab_url:
            return "colab"
        if self._ssh_config is not None:
            return "ssh-remote"
        if self._modal_token:
            return "modal"
        return "unavailable"

    def available(self) -> bool:
        if self._colab_url:
            return True
        if self._ssh_config is not None and self._ssh_config.allowed():
            return True
        if self._modal_token:
            return True
        return False

    def health(self) -> dict[str, Any]:
        """Structured provider health (Phase 11 vocabulary)."""
        if self.available():
            return {"provider": f"compute-{self.backend_name}",
                    "status": "AVAILABLE",
                    "configured": self.backend_name,
                    "note": "Configuration present; per-call status is "
                            "reported in each execution result."}
        return {
            "provider": "compute-remote",
            "status": "MISCONFIGURED" if (
                self._colab_url or self._modal_token
                or os.environ.get("FORGE_COMPUTE_SSH_HOST", "").strip())
            else "UNAVAILABLE",
            "configured": False,
            "note": self.ssh_config_error or (
                "Set FORGE_COLAB_URL, FORGE_COMPUTE_SSH_HOST + "
                "FORGE_COMPUTE_SSH_ALLOWLIST, or MODAL_TOKEN_ID."),
        }

    def execute(self, code: str, *,
                timeout: float = 30.0) -> dict[str, Any]:
        """Execute code on the configured remote backend."""
        if not isinstance(code, str):
            return {"status": "failed", "output": "code must be a string",
                    "output_truncated": False, "elapsed_ms": 0.0,
                    "backend": "unavailable", "timed_out": False}
        code = code.strip()[:MAX_CODE]
        if not code or "\x00" in code:
            return {"status": "refused",
                    "output": "Empty or invalid code refused.",
                    "output_truncated": False, "elapsed_ms": 0.0,
                    "backend": "unavailable", "timed_out": False}
        if self._colab_url:
            return self._execute_colab(code, timeout)
        if self._ssh_config is not None:
            if not self._ssh_config.allowed():
                return {"status": "refused",
                        "output": ("SSH destination refused: not on "
                                   "FORGE_COMPUTE_SSH_ALLOWLIST"),
                        "output_truncated": False, "elapsed_ms": 0.0,
                        "backend": "ssh-remote", "timed_out": False}
            # Destination-IP policy: resolve + classify every address
            # before the transport may start (fail-closed).
            try:
                ssh_destination_policy(self._ssh_config)
            except RemoteComputeError as exc:
                return {"status": "refused",
                        "output": str(exc),
                        "output_truncated": False, "elapsed_ms": 0.0,
                        "backend": "ssh-remote", "timed_out": False}
            return _execute_ssh_secure(code, self._ssh_config, timeout)
        if self._modal_token:
            return self._execute_modal(code, timeout)
        return {"status": "refused",
                "output": ("No remote compute backend configured. Set "
                           "FORGE_COLAB_URL, FORGE_COMPUTE_SSH_HOST + "
                           "FORGE_COMPUTE_SSH_ALLOWLIST, or "
                           "MODAL_TOKEN_ID."),
                "output_truncated": False, "elapsed_ms": 0.0,
                "backend": "unavailable", "timed_out": False}

    # -- Kernel proxy (Colab-style JSON API) --------------------------------

    def _colab_endpoint(self) -> str:
        """Normalize + validate the configured kernel-proxy URL.

        Delegates to :func:`colab_url_policy` (parse -> normalize ->
        policy -> DNS/IP safety), so the same enforcement protects the
        backend and any other consumer of ``FORGE_COLAB_URL``.
        """
        return colab_url_policy(self._colab_url)

    def _execute_colab(self, code: str, timeout: float) -> dict[str, Any]:
        """Execute code via a Colab notebook kernel proxy (SSRF-safe)."""
        import json
        import urllib.request
        import urllib.error

        started = time.monotonic()
        try:
            url = self._colab_endpoint()
            body = json.dumps({
                "code": code,
                "timeout": int(timeout),
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{url}/execute", data=body, method="POST",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(
                    req, timeout=timeout + 5) as resp:
                raw = resp.read(2_000_000)  # bounded response
                data = json.loads(raw.decode("utf-8"))
            elapsed = time.monotonic() - started
            output = str(data.get("output", ""))[:MAX_OUTPUT]
            return {
                "status": "succeeded" if data.get("success", False)
                          else "failed",
                "output": output,
                "output_truncated": len(output) > MAX_OUTPUT,
                "elapsed_ms": round(elapsed * 1000, 1),
                "backend": "colab",
                "timed_out": False,
            }
        except RemoteComputeError as exc:
            return {"status": "refused", "output": str(exc),
                    "output_truncated": False, "elapsed_ms": 0.0,
                    "backend": "colab", "timed_out": False}
        except urllib.error.HTTPError as exc:
            state = "timeout" if exc.code == 408 else "failed"
            return {"status": state,
                    "output": f"Colab HTTP {exc.code}: {exc.reason}",
                    "output_truncated": False,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                    "backend": "colab", "timed_out": state == "timeout"}
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            timed_out = isinstance(reason, TimeoutError) or \
                "timed out" in str(reason)
            return {"status": "timeout" if timed_out else "failed",
                    "output": f"Colab connection error: {reason}",
                    "output_truncated": False,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                    "backend": "colab", "timed_out": timed_out}
        except Exception as exc:  # noqa: BLE001 — transport-level catch-all
            return {"status": "failed", "output": f"Colab error: {exc}",
                    "output_truncated": False,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                    "backend": "colab", "timed_out": False}

    # -- Modal ----------------------------------------------------------------

    def _execute_modal(self, code: str, timeout: float) -> dict[str, Any]:
        """Execute code inside a Modal sandbox (arg-list transport)."""
        started = time.monotonic()
        try:
            import modal  # type: ignore[import-not-found]
            if not self._modal_secret:
                return {"status": "failed",
                        "output": ("MODAL_TOKEN_SECRET is required for "
                                   "Modal compute."),
                        "output_truncated": False, "elapsed_ms": 0.0,
                        "backend": "modal", "timed_out": False}
            app = modal.App.lookup("forge-compute", create_if_missing=True)
            with modal.Sandbox.create(
                    app=app, timeout=int(timeout)) as sandbox:
                proc = sandbox.exec("python3", "-I", "-c", code)
                stdout = proc.stdout.read() or ""
                stderr = proc.stderr.read() or ""
                exit_code = proc.wait()
            elapsed = time.monotonic() - started
            output = (str(stdout) + str(stderr))[:MAX_OUTPUT]
            return {
                "status": "succeeded" if exit_code == 0 else "failed",
                "output": output,
                "output_truncated": len(output) > MAX_OUTPUT,
                "elapsed_ms": round(elapsed * 1000, 1),
                "backend": "modal",
                "timed_out": False,
            }
        except ImportError:
            return {
                "status": "failed",
                "output": ("Modal SDK not installed. "
                           "Run: pip install modal"),
                "output_truncated": False,
                "elapsed_ms": 0.0,
                "backend": "modal",
                "timed_out": False,
            }
        except Exception as exc:  # noqa: BLE001 — SDK-specific errors
            elapsed = time.monotonic() - started
            return {
                "status": "failed",
                "output": f"Modal error: {exc}",
                "output_truncated": False,
                "elapsed_ms": round(elapsed * 1000, 1),
                "backend": "modal",
                "timed_out": False,
            }
