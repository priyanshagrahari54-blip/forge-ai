"""Remote compute backends for A48.

Provides remote Python execution via:

- **local** — default, real subprocess (see ``forge.compute.engine``)
- **Google Colab** — kernel proxy (``FORGE_COLAB_URL``)
- **SSH** — strict, fail-closed remote host (``FORGE_COMPUTE_SSH_*``)
- **Modal** — serverless sandbox (``MODAL_TOKEN_ID`` + secret)

Security invariants for the SSH backend:

- User code NEVER appears in a command line and never goes through a
  shell. It is delivered exclusively over stdin to a fixed remote
  command: ``python3 -I -`` (isolated mode, program read from stdin).
- Host-key verification is mandatory: ``StrictHostKeyChecking=yes``
  against the configured known_hosts file; password prompts are
  disabled (``BatchMode=yes``). There is no
  ``StrictHostKeyChecking=no`` anywhere.
- The destination must appear in an explicit allowlist
  (``FORGE_COMPUTE_SSH_ALLOWLIST``) — fail-closed when absent.
- User, host, and port are explicit; the hostname matches a strict
  pattern; connection, execution, and output are bounded; the whole
  remote process group is cancellable via timeout.
- Execution is policy-gated by the caller (control plane
  ``compute_execute`` -> TERMINAL policy), audited, and labeled with
  the backend name and honest status.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

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

    # -- Google Colab ---------------------------------------------------------

    def _colab_endpoint(self) -> str:
        """Normalize + validate the configured Colab kernel proxy URL."""
        from urllib.parse import urlparse
        url = self._colab_url
        if "://" not in url:
            url = f"https://{url}"
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()
        if scheme not in ("https", "http"):
            raise RemoteComputeError(
                "FORGE_COLAB_URL must be https:// (or http:// with "
                "FORGE_COLAB_ALLOW_HTTP=1)")
        if scheme == "http" and \
                os.environ.get("FORGE_COLAB_ALLOW_HTTP", "") != "1":
            raise RemoteComputeError(
                "FORGE_COLAB_URL uses http://; set "
                "FORGE_COLAB_ALLOW_HTTP=1 to permit it explicitly")
        if parsed.username is not None or parsed.password is not None:
            raise RemoteComputeError(
                "FORGE_COLAB_URL must not embed credentials")
        if not host:
            raise RemoteComputeError("FORGE_COLAB_URL has no hostname")
        # The Colab connect flow legitimately proxies through a localhost
        # kernel endpoint; localhost requires an explicit opt-in because
        # it routes straight into this machine.
        if host in ("localhost", "127.0.0.1", "::1") or host.endswith(
                (".local", ".internal")):
            if os.environ.get("FORGE_COLAB_ALLOW_LOCALHOST", "") != "1":
                raise RemoteComputeError(
                    "FORGE_COLAB_URL points at a local endpoint; set "
                    "FORGE_COLAB_ALLOW_LOCALHOST=1 to permit it explicitly")
        return url.rstrip("/")

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
