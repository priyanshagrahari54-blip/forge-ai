"""Production deployment backends for A64.

Extends the local staging deployment with real production targets:

* **Docker**: Build and push container images
* **Kubernetes**: Deploy to K8s clusters via kubectl
* **SSH/rsync**: Deploy to remote servers
* **Fly.io**: Deploy via Fly CLI

All production deployments are permission-gated, build on the same
manifest/artifact system, and require explicit configuration.
"""
from __future__ import annotations

import os
import subprocess
import time
from typing import Any
from pathlib import Path


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
                capture_output=True, text=True, timeout=5)
            return proc.returncode == 0
        except Exception:
            return False

    def build(self, context: str, tag: str,
              dockerfile: str = "Dockerfile") -> dict[str, Any]:
        """Build a Docker image from a deployment artifact."""
        started = time.time()
        try:
            cmd = ["docker", "build", "-t", tag, "-f", dockerfile, "."]
            proc = subprocess.run(
                cmd, cwd=context, capture_output=True,
                text=True, timeout=300)
            elapsed = time.time() - started
            return {
                "success": proc.returncode == 0,
                "tag": tag,
                "output": (proc.stdout + proc.stderr)[:4000],
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
                capture_output=True, text=True, timeout=300)
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
                capture_output=True, text=True, timeout=5)
            return proc.returncode == 0
        except Exception:
            return False

    def apply(self, manifest_path: str) -> dict[str, Any]:
        """Apply a Kubernetes manifest."""
        try:
            proc = subprocess.run(
                ["kubectl", "apply", "-f", manifest_path,
                 "-n", self._namespace],
                capture_output=True, text=True, timeout=60)
            return {
                "success": proc.returncode == 0,
                "output": (proc.stdout + proc.stderr)[:4000],
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
                timeout=timeout + 5)
            return {
                "success": proc.returncode == 0,
                "output": (proc.stdout + proc.stderr)[:2000],
                "backend": "kubernetes",
            }
        except Exception as exc:
            return {"success": False, "error": str(exc),
                    "backend": "kubernetes"}


class SSHDeployer:
    """Deploy to a remote server via rsync over SSH."""

    def __init__(self) -> None:
        self._host = os.environ.get("FORGE_DEPLOY_SSH_HOST", "")
        self._path = os.environ.get("FORGE_DEPLOY_SSH_PATH", "")

    def available(self) -> bool:
        return bool(self._host and self._path)

    def deploy(self, source_dir: str) -> dict[str, Any]:
        """Deploy via rsync to the configured SSH host."""
        if not self.available():
            return {"success": False,
                    "error": "No FORGE_DEPLOY_SSH_HOST/PATH configured"}
        started = time.time()
        try:
            dest = f"{self._host}:{self._path}"
            proc = subprocess.run(
                ["rsync", "-avz", "--delete",
                 f"{source_dir}/", dest],
                capture_output=True, text=True, timeout=300)
            elapsed = time.time() - started
            return {
                "success": proc.returncode == 0,
                "output": (proc.stdout + proc.stderr)[:4000],
                "elapsed_ms": round(elapsed * 1000, 1),
                "target": dest,
                "backend": "ssh-rsync",
            }
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
                capture_output=True, text=True, timeout=5)
            return proc.returncode == 0 and bool(self._token)
        except Exception:
            return False

    def deploy(self, app_name: str, *,
               working_dir: str = ".") -> dict[str, Any]:
        """Deploy to a Fly.io app."""
        env = dict(os.environ)
        if self._token:
            env["FLY_API_TOKEN"] = self._token
        started = time.time()
        try:
            proc = subprocess.run(
                ["flyctl", "deploy", "--app", app_name,
                 "--remote-only"],
                cwd=working_dir, capture_output=True,
                text=True, timeout=600, env=env)
            elapsed = time.time() - started
            return {
                "success": proc.returncode == 0,
                "app": app_name,
                "output": (proc.stdout + proc.stderr)[:4000],
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
    }
    backends["fly-io"] = {
        "available": fly.available(),
    }
    return backends
