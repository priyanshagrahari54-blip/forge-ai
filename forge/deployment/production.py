"""Production deployment backends for A64.

Extends the local staging deployment with real production targets:

* **Docker**: Build and push container images
* **Kubernetes**: Deploy to K8s clusters via kubectl
* **SSH/rsync**: Deploy to remote servers (hardened transport)
* **Fly.io**: Deploy via Fly CLI

Security invariants for remote (SSH/rsync) deployment:

* Destination identity is explicit (user@host:path) and MUST appear on
  ``FORGE_DEPLOY_SSH_ALLOWLIST`` — deployment is refused otherwise.
* Destination-IP policy (shared with remote compute): before rsync
  starts, the hostname is resolved and EVERY returned address is
  classified. Non-public addresses refuse the deployment unless the
  operator explicitly opted in with
  ``FORGE_DEPLOY_SSH_ALLOW_PRIVATE=1``; unresolvable hostnames fail
  closed; the never-routed set (cloud metadata, multicast,
  documentation/benchmark/reserved ranges) is refused even with the
  opt-in. A hostname on the allowlist never bypasses this policy.
* ``rsync --delete`` NEVER runs unless (a) the caller explicitly
  requests deletion AND (b) the caller passes a validated approval
  token (``delete_approved=True``). The deployer itself refuses
  ``--delete`` without that flag — an agent cannot delete remotely by
  flipping a single argument.
* SSH options are strict: ``StrictHostKeyChecking=yes`` against the
  configured known_hosts, ``BatchMode=yes``, no password prompts, no
  ``StrictHostKeyChecking=no``, no shell interpolation: user/path
  values are validated against strict patterns and argv lists are
  built without a shell.
* After the transfer, artifacts are verified remotely by comparing
  SHA-256 hashes of deployed files; a failed verification is reported
  honestly (never a silent success).
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import time
from typing import Any
from pathlib import Path

from forge.compute.remote import (
    RemoteComputeError as _DestinationPolicyError,
    destination_ip_policy,
)

_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,252})$")
_USER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_PATH_RE = re.compile(r"^/(?:[A-Za-z0-9._~/-]{1,900}|)$|^~/(?:[A-Za-z0-9._~/-]{1,900})$")  # noqa: E501
_ALLOWLIST_RE = re.compile(
    r"^(?:(?P<user>[A-Za-z0-9._-]{1,64})@)?"
    r"(?P<host>[A-Za-z0-9](?:[A-Za-z0-9._-]{0,252}))$")

MAX_DEPLOY_OUTPUT = 4000


class RemoteDeployError(RuntimeError):
    """Configuration/transport failure for a remote deployment target."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_ssh_target(*, host_env: str, user_env: str = "", path_env: str = "",
                     allowlist_env: str, port_env: str = "",
                     identity_env: str = "",
                     known_hosts_env: str = "") -> dict[str, Any]:
    """Parse and validate the SSH deployment target. Fail-closed."""
    spec = (host_env or "").strip()
    if not spec:
        raise RemoteDeployError(
            "FORGE_DEPLOY_SSH_HOST is not configured")
    user = ""
    host = ""
    port = 22
    if "@" in spec:
        user_part, _, host_part = spec.rpartition("@")
        user = user_part
    else:
        host_part = spec
    if ":" in host_part:
        host_text, _, port_text = host_part.rpartition(":")
        if not port_text.isdigit():
            raise RemoteDeployError(
                "FORGE_DEPLOY_SSH_HOST port must be numeric")
        port = int(port_text)
        host_part = host_text
    user = user or user_env.strip()
    if not user or not _USER_RE.match(user):
        raise RemoteDeployError(
            "SSH deployment requires an explicit valid user")
    host = host_part
    if not host or not _HOST_RE.match(host):
        raise RemoteDeployError(f"invalid SSH deploy host {host!r}")
    if not (0 < port < 65536):
        raise RemoteDeployError(f"invalid SSH port {port}")
    path = (path_env or "").strip()
    if not path or not _PATH_RE.match(path):
        raise RemoteDeployError(
            "FORGE_DEPLOY_SSH_PATH must be an absolute path "
            "(or ~/...) without shell metacharacters")
    allowlist_raw = (allowlist_env or "").strip()
    if not allowlist_raw:
        raise RemoteDeployError(
            "SSH deployment refused: FORGE_DEPLOY_SSH_ALLOWLIST must "
            "explicitly list the destination (fail-closed)")
    allowlist = tuple(
        e.strip() for e in allowlist_raw.split(",") if e.strip())
    for entry in allowlist:
        if not _ALLOWLIST_RE.match(entry):
            raise RemoteDeployError(f"invalid allowlist entry {entry!r}")
    target = f"{user}@{host}"
    if host not in allowlist and target not in allowlist:
        raise RemoteDeployError(
            f"deploy destination {target} is not on the "
            "FORGE_DEPLOY_SSH_ALLOWLIST")
    identity = identity_env.strip()
    known_hosts = (known_hosts_env.strip() or os.path.join(
        os.path.expanduser("~"), ".ssh", "known_hosts"))
    if identity and not re.match(r"^[A-Za-z0-9_./~-]{1,500}$", identity):
        raise RemoteDeployError("invalid SSH identity path")
    if not re.match(r"^[A-Za-z0-9_./~-]{1,500}$", known_hosts):
        raise RemoteDeployError("invalid known_hosts path")
    return {"user": user, "host": host, "port": port, "path": path,
            "identity": identity, "known_hosts": known_hosts}


def _ssh_base_argv(target: dict[str, Any]) -> list[str]:
    """Fixed, strict ssh options; no user-controlled shell metachars."""
    argv = [
        "ssh",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={target['known_hosts']}",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        "-o", "LogLevel=ERROR",
        "-o", "IdentitiesOnly=yes",
    ]
    if target["port"] != 22:
        argv += ["-p", str(target["port"])]
    if target["identity"]:
        argv += ["-i", target["identity"]]
    argv += [f"{target['user']}@{target['host']}"]
    return argv


#: Operator opt-in that permits rsync deployment to a destination
#: resolving to non-public addresses (never the never-routed set).
_DEPLOY_PRIVATE_EXCEPTION_ENV = "FORGE_DEPLOY_SSH_ALLOW_PRIVATE"


def _deploy_destination_policy(target: dict[str, Any]) -> None:
    """Resolved-address policy gate for an rsync deployment target.

    Resolves ``target['host']`` and classifies every address; raises
    :class:`RemoteDeployError` (fail-closed) when the host is
    unresolvable or any address is non-public without the explicit
    operator opt-in ``FORGE_DEPLOY_SSH_ALLOW_PRIVATE=1``.
    """
    try:
        destination_ip_policy(
            target["host"], purpose="SSH deploy",
            opt_in_env=_DEPLOY_PRIVATE_EXCEPTION_ENV,
            hint=(f"if this is an explicit operator-controlled "
                  f"private-network deploy destination, set "
                  f"{_DEPLOY_PRIVATE_EXCEPTION_ENV}=1 (the host must "
                  "still be on FORGE_DEPLOY_SSH_ALLOWLIST)"))
    except _DestinationPolicyError as exc:
        raise RemoteDeployError(str(exc)) from exc


class DockerDeployer:
    """Deploy via Docker container build + push."""

    def __init__(self) -> None:
        self._registry = os.environ.get(
            "FORGE_DOCKER_REGISTRY", "")
        self._username = os.environ.get(
            "FORGE_DOCKER_USERNAME", "")

    def available(self) -> bool:
        try:
            proc = subprocess.run(
                ["docker", "--version"],
                capture_output=True, text=True, timeout=5,
                check=False)
            return proc.returncode == 0
        except Exception:
            return False

    def build(self, context: str, tag: str,
              dockerfile: str = "Dockerfile") -> dict[str, Any]:
        """Build a Docker image from a deployment artifact."""
        if not tag or not re.match(r"^[A-Za-z0-9._/-]{1,128}:"
                                   r"[A-Za-z0-9._-]{1,128}$", tag):
            return {"success": False,
                    "error": "invalid image tag", "backend": "docker"}
        if not dockerfile or "/" in dockerfile or ".." in dockerfile \
                or not re.match(r"^[A-Za-z0-9._-]{1,128}$", dockerfile):
            return {"success": False,
                    "error": "invalid Dockerfile name", "backend": "docker"}
        started = time.time()
        try:
            cmd = ["docker", "build", "-t", tag, "-f", dockerfile, "."]
            proc = subprocess.run(
                cmd, cwd=context, capture_output=True,
                text=True, timeout=300,
                check=False)
            elapsed = time.time() - started
            return {
                "success": proc.returncode == 0,
                "tag": tag,
                "output": (proc.stdout + proc.stderr)[:MAX_DEPLOY_OUTPUT],
                "elapsed_ms": round(elapsed * 1000, 1),
                "backend": "docker",
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Build timed out",
                    "backend": "docker"}
        except Exception as exc:
            return {"success": False, "error": str(exc),
                    "backend": "docker"}

    def push(self, tag: str) -> dict[str, Any]:
        """Push a Docker image to the configured registry."""
        if not self._registry:
            return {"success": False,
                    "error": "No FORGE_DOCKER_REGISTRY configured"}
        full_tag = f"{self._registry}/{tag}" if "/" not in tag else tag
        try:
            proc = subprocess.run(
                ["docker", "push", full_tag],
                capture_output=True, text=True, timeout=300,
                check=False)
            return {
                "success": proc.returncode == 0,
                "tag": full_tag,
                "output": (proc.stdout + proc.stderr)[:2000],
                "backend": "docker",
            }
        except Exception as exc:
            return {"success": False, "error": str(exc),
                    "backend": "docker"}


class KubernetesDeployer:
    """Deploy to Kubernetes via kubectl."""

    def __init__(self) -> None:
        self._namespace = os.environ.get(
            "FORGE_K8S_NAMESPACE", "default")

    def available(self) -> bool:
        try:
            proc = subprocess.run(
                ["kubectl", "version", "--client"],
                capture_output=True, text=True, timeout=5,
                check=False)
            return proc.returncode == 0
        except Exception:
            return False

    def apply(self, manifest_path: str) -> dict[str, Any]:
        """Apply a Kubernetes manifest."""
        try:
            proc = subprocess.run(
                ["kubectl", "apply", "-f", manifest_path,
                 "-n", self._namespace],
                capture_output=True, text=True, timeout=60,
                check=False)
            return {
                "success": proc.returncode == 0,
                "output": (proc.stdout + proc.stderr)[:MAX_DEPLOY_OUTPUT],
                "namespace": self._namespace,
                "backend": "kubernetes",
            }
        except Exception as exc:
            return {"success": False, "error": str(exc),
                    "backend": "kubernetes"}

    def rollout_status(self, deployment_name: str,
                       timeout: int = 120) -> dict[str, Any]:
        """Check rollout status of a K8s deployment."""
        try:
            proc = subprocess.run(
                ["kubectl", "rollout", "status",
                 f"deployment/{deployment_name}",
                 "-n", self._namespace,
                 f"--timeout={timeout}s"],
                capture_output=True, text=True,
                timeout=timeout + 5,
                check=False)
            return {
                "success": proc.returncode == 0,
                "output": (proc.stdout + proc.stderr)[:2000],
                "backend": "kubernetes",
            }
        except Exception as exc:
            return {"success": False, "error": str(exc),
                    "backend": "kubernetes"}


class SSHDeployer:
    """Deploy to a remote server via rsync over a hardened SSH transport.

    Fail-closed requirements: allowlisted destination, strict host-key
    verification, no shell interpolation, and ``--delete`` only with an
    explicit approved flag. Post-transfer SHA-256 verification runs
    over ssh argv (no shell) against the recorded manifest.
    """

    def __init__(self) -> None:
        self._host_env = os.environ.get("FORGE_DEPLOY_SSH_HOST", "")
        self._user_env = os.environ.get("FORGE_DEPLOY_SSH_USER", "")
        self._path_env = os.environ.get("FORGE_DEPLOY_SSH_PATH", "")
        self._port_env = os.environ.get("FORGE_DEPLOY_SSH_PORT", "")
        self._allowlist_env = os.environ.get(
            "FORGE_DEPLOY_SSH_ALLOWLIST", "")
        self._identity_env = os.environ.get(
            "FORGE_DEPLOY_SSH_IDENTITY", "")
        self._known_hosts_env = os.environ.get(
            "FORGE_DEPLOY_SSH_KNOWN_HOSTS", "")
        self._target: dict[str, Any] | None = None
        self._config_error = ""
        try:
            self._target = parse_ssh_target(
                host_env=self._host_env, user_env=self._user_env,
                path_env=self._path_env, port_env=self._port_env,
                allowlist_env=self._allowlist_env,
                identity_env=self._identity_env,
                known_hosts_env=self._known_hosts_env)
        except RemoteDeployError as exc:
            self._config_error = str(exc)

    def available(self) -> bool:
        return self._target is not None

    @property
    def config_error(self) -> str:
        return self._config_error

    def health(self) -> dict[str, Any]:
        if self._target is None:
            return {"provider": "deploy-ssh-rsync",
                    "status": "MISCONFIGURED" if self._config_error
                    else "UNAVAILABLE",
                    "configured": False, "note": self._config_error or
                    "Set FORGE_DEPLOY_SSH_HOST/PATH/ALLOWLIST."}
        return {"provider": "deploy-ssh-rsync", "status": "AVAILABLE",
                "configured": f"{self._target['user']}@"
                              f"{self._target['host']}:"
                              f"{self._target['path']}",
                "note": "Configuration present; per-deploy results are "
                        "reported in each result."}

    def _rsync_rsh(self) -> str:
        target = self._target
        assert target is not None
        tokens = [
            "ssh", "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={target['known_hosts']}",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            "-o", "LogLevel=ERROR", "-o", "IdentitiesOnly=yes",
        ]
        if target["port"] != 22:
            tokens += ["-p", str(target["port"])]
        if target["identity"]:
            tokens += ["-i", target["identity"]]
        # rsync -e executes this string through a shell; every token is
        # either a constant or a pattern-validated config value, and is
        # shell-quoted here as defense in depth.
        return " ".join(shlex.quote(token) for token in tokens)

    def _remote_sha256(self, rel_paths: list[str]) -> dict[str, str]:
        """Hash files on the remote host via ssh argv (no shell)."""
        target = self._target
        assert target is not None
        argv = _ssh_base_argv(target) + ["sha256sum"] + [
            f"{target['path'].rstrip('/')}/{rel}" for rel in rel_paths]
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=60,
                              check=False)
        if proc.returncode != 0:
            return {}
        hashes: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                hashes[parts[1].strip()] = parts[0].strip().lower()
        return hashes

    def verify(self, source_dir: str, checks: list[str]) -> dict[str, Any]:
        """Verify deployed files by remote SHA-256 comparison."""
        source = Path(source_dir)
        local: dict[str, str] = {}
        for rel in checks:
            candidate = source / rel
            if candidate.is_file():
                local[rel] = _sha256_file(candidate)
        if not local:
            return {"verified": False,
                    "reason": "no local files available for verification",
                    "backend": "ssh-rsync"}
        rels = list(local)
        remote = self._remote_sha256(rels)
        mismatches = []
        missing = []
        for rel in rels:
            remote_rel = f"{self._target['path'].rstrip('/')}/{rel}"
            if remote_rel not in remote:
                missing.append(rel)
            elif remote[remote_rel] != local[rel]:
                mismatches.append(rel)
        return {"verified": not mismatches and not missing,
                "checked": len(rels),
                "mismatches": mismatches[:20],
                "missing": missing[:20],
                "backend": "ssh-rsync"}

    def deploy(self, source_dir: str, *,
               delete: bool = False,
               delete_approved: bool = False,
               verify_checks: list[str] | None = None,
               timeout: int = 300) -> dict[str, Any]:
        """Deploy ``source_dir`` via rsync to the allowlisted target.

        ``delete`` (rsync --delete) requires ``delete_approved=True``,
        which only the control plane sets after a validated approval
        token. ``verify_checks`` are relative file paths hashed locally
        and remotely after the transfer.
        """
        if self._target is None:
            return {"success": False,
                    "error": self._config_error or
                    "No valid FORGE_DEPLOY_SSH_HOST/PATH configured",
                    "backend": "ssh-rsync"}
        if delete and not delete_approved:
            return {"success": False,
                    "error": ("destructive deployment (--delete) refused: "
                              "requires an approved token"),
                    "backend": "ssh-rsync"}
        # Destination-IP policy: resolve + classify every address before
        # rsync may start (fail-closed, allowlist alone never bypasses).
        try:
            _deploy_destination_policy(self._target)
        except RemoteDeployError as exc:
            return {"success": False, "error": str(exc),
                    "backend": "ssh-rsync"}
        source = Path(source_dir)
        if not source.is_dir():
            return {"success": False,
                    "error": f"source directory {source_dir!r} not found",
                    "backend": "ssh-rsync"}
        started = time.time()
        dest = f"{self._target['user']}@{self._target['host']}:" \
               f"{self._target['path'].rstrip('/')}/"
        cmd = ["rsync", "-a", "-z", "-e", self._rsync_rsh(),
               f"{source.resolve()}/", dest]
        if delete:
            cmd.append("--delete")
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                check=False)
            elapsed = time.time() - started
            base = {
                "success": proc.returncode == 0,
                "output": (proc.stdout + proc.stderr)[:MAX_DEPLOY_OUTPUT],
                "elapsed_ms": round(elapsed * 1000, 1),
                "target": f"{self._target['user']}@{self._target['host']}:"
                          f"{self._target['path']}",
                "delete": bool(delete),
                "backend": "ssh-rsync",
            }
            if proc.returncode != 0:
                base["error"] = (proc.stderr or proc.stdout)[:2000]
                return base
            verification = self.verify(
                source_dir,
                verify_checks or []) if verify_checks else {
                    "verified": None}
            base["verification"] = verification
            if verification.get("verified") is False:
                base["success"] = False
                base["error"] = (
                    "rsync completed but post-deploy verification failed: "
                    f"{verification.get('reason') or ''} "
                    f"mismatches={verification.get('mismatches', [])} "
                    f"missing={verification.get('missing', [])}")
            return base
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Deploy timed out",
                    "backend": "ssh-rsync"}
        except Exception as exc:
            return {"success": False, "error": str(exc),
                    "backend": "ssh-rsync"}


class FlyDeployer:
    """Deploy to Fly.io via the Fly CLI."""

    def __init__(self) -> None:
        self._token = os.environ.get("FLY_API_TOKEN", "")

    def available(self) -> bool:
        try:
            proc = subprocess.run(
                ["flyctl", "version"],
                capture_output=True, text=True, timeout=5,
                check=False)
            return proc.returncode == 0 and bool(self._token)
        except Exception:
            return False

    def health(self) -> dict[str, Any]:
        return {"provider": "deploy-fly-io",
                "status": "AVAILABLE" if self.available()
                else "MISCONFIGURED" if self._token
                else "UNAVAILABLE",
                "configured": bool(self._token),
                "note": "" if self.available() else
                "Requires flyctl on PATH and FLY_API_TOKEN."}

    def deploy(self, app_name: str, *,
               working_dir: str = ".") -> dict[str, Any]:
        """Deploy to a Fly.io app."""
        if not app_name or not re.match(
                r"^[a-z0-9][a-z0-9-]{0,63}$", app_name):
            return {"success": False,
                    "error": "invalid fly app name", "backend": "fly-io"}
        env = dict(os.environ)
        if self._token:
            env["FLY_API_TOKEN"] = self._token
        started = time.time()
        try:
            proc = subprocess.run(
                ["flyctl", "deploy", "--app", app_name,
                 "--remote-only"],
                cwd=working_dir, capture_output=True,
                text=True, timeout=600, env=env,
                check=False)
            elapsed = time.time() - started
            return {
                "success": proc.returncode == 0,
                "app": app_name,
                "output": (proc.stdout + proc.stderr)[:MAX_DEPLOY_OUTPUT],
                "elapsed_ms": round(elapsed * 1000, 1),
                "backend": "fly-io",
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Deploy timed out",
                    "backend": "fly-io"}
        except Exception as exc:
            return {"success": False, "error": str(exc),
                    "backend": "fly-io"}


def available_backends() -> dict[str, dict[str, Any]]:
    """Report which production deployment backends are available."""
    backends = {}
    docker = DockerDeployer()
    k8s = KubernetesDeployer()
    ssh = SSHDeployer()
    fly = FlyDeployer()
    backends["docker"] = {
        "available": docker.available(),
        "registry": os.environ.get("FORGE_DOCKER_REGISTRY", ""),
    }
    backends["kubernetes"] = {
        "available": k8s.available(),
        "namespace": os.environ.get("FORGE_K8S_NAMESPACE", "default"),
    }
    backends["ssh-rsync"] = {
        "available": ssh.available(),
        "configured": ssh.available(),
        "note": ssh.config_error or "",
    }
    backends["fly-io"] = {
        "available": fly.available(),
        "health": fly.health(),
    }
    return backends
