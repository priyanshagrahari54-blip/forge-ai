"""Remote compute backend for A48.

Provides remote Python execution via:
- Google Colab (when FORGE_COLAB_URL is set)
- SSH remote (when FORGE_COMPUTE_SSH_HOST is set)
- Modal (when MODAL_TOKEN_ID is set)

All remote backends are labeled honestly and require explicit
configuration. The same permission model applies: Resource.TERMINAL /
execute policy gate, same quotas, same bounded output.
"""
from __future__ import annotations

import os
import time
from typing import Any

MAX_CODE = 6000
MAX_OUTPUT = 4000
REQUEST_TIMEOUT = 60.0


class RemoteComputeBackend:
    """Execute Python code on a remote backend.

    Supports Google Colab, SSH, and Modal. Returns ``available()``
    only when at least one backend is configured.
    """

    def __init__(self) -> None:
        self._colab_url = os.environ.get("FORGE_COLAB_URL", "")
        self._ssh_host = os.environ.get("FORGE_COMPUTE_SSH_HOST", "")
        self._modal_token = os.environ.get("MODAL_TOKEN_ID", "")

    @property
    def backend_name(self) -> str:
        if self._colab_url:
            return "colab"
        if self._ssh_host:
            return "ssh-remote"
        if self._modal_token:
            return "modal"
        return "unavailable"

    def available(self) -> bool:
        return bool(self._colab_url or self._ssh_host or self._modal_token)

    def execute(self, code: str, *,
                timeout: float = 30.0) -> dict[str, Any]:
        """Execute code on the configured remote backend."""
        code = code.strip()[:MAX_CODE]
        if self._colab_url:
            return self._execute_colab(code, timeout)
        if self._ssh_host:
            return self._execute_ssh(code, timeout)
        if self._modal_token:
            return self._execute_modal(code, timeout)
        return {"status": "refused",
                "output": "No remote compute backend configured.",
                "backend": "unavailable"}

    def _execute_colab(self, code: str, timeout: float) -> dict[str, Any]:
        """Execute code via a Colab notebook kernel proxy.

        Requires FORGE_COLAB_URL to point to the Colab kernel proxy
        endpoint (obtained via the Colab connect flow).
        """
        import json
        import urllib.request
        import urllib.error

        started = time.monotonic()
        url = self._colab_url.rstrip("/")
        try:
            body = json.dumps({
                "code": code,
                "timeout": int(timeout),
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{url}/execute", data=body, method="POST",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(
                    req, timeout=timeout + 5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            elapsed = time.monotonic() - started
            return {
                "status": "succeeded" if data.get("success", False)
                          else "failed",
                "output": str(data.get("output", ""))[:MAX_OUTPUT],
                "output_truncated": len(str(data.get("output", ""))) >
                                    MAX_OUTPUT,
                "elapsed_ms": round(elapsed * 1000, 1),
                "backend": "colab",
                "timed_out": False,
            }
        except urllib.error.URLError as exc:
            elapsed = time.monotonic() - started
            return {
                "status": "failed",
                "output": f"Colab connection error: {exc.reason}",
                "output_truncated": False,
                "elapsed_ms": round(elapsed * 1000, 1),
                "backend": "colab",
                "timed_out": False,
            }
        except Exception as exc:
            elapsed = time.monotonic() - started
            return {
                "status": "failed",
                "output": f"Colab error: {exc}",
                "output_truncated": False,
                "elapsed_ms": round(elapsed * 1000, 1),
                "backend": "colab",
                "timed_out": False,
            }

    def _execute_ssh(self, code: str, timeout: float) -> dict[str, Any]:
        """Execute code on a remote host via SSH.

        Requires FORGE_COMPUTE_SSH_HOST (e.g., 'user@host').
        """
        import subprocess
        import tempfile

        host = self._ssh_host
        started = time.monotonic()
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".py", delete=False) as f:
                f.write(code)
                temp_path = f.name
            proc = subprocess.run(
                ["ssh", "-o", "StrictHostKeyChecking=no",
                 "-o", "ConnectTimeout=10",
                 host, "python3", "-I", "-c", f"'{code}'"],
                capture_output=True, text=True,
                timeout=timeout + 5)
            elapsed = time.monotonic() - started
            output = (proc.stdout or "") + (proc.stderr or "")
            return {
                "status": "succeeded" if proc.returncode == 0
                          else "failed",
                "output": output[:MAX_OUTPUT],
                "output_truncated": len(output) > MAX_OUTPUT,
                "elapsed_ms": round(elapsed * 1000, 1),
                "backend": "ssh-remote",
                "timed_out": False,
            }
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started
            return {
                "status": "timeout",
                "output": f"SSH execution timed out after {timeout}s",
                "output_truncated": False,
                "elapsed_ms": round(elapsed * 1000, 1),
                "backend": "ssh-remote",
                "timed_out": True,
            }
        except Exception as exc:
            elapsed = time.monotonic() - started
            return {
                "status": "failed",
                "output": f"SSH error: {exc}",
                "output_truncated": False,
                "elapsed_ms": round(elapsed * 1000, 1),
                "backend": "ssh-remote",
                "timed_out": False,
            }

    def _execute_modal(self, code: str, timeout: float) -> dict[str, Any]:
        """Execute code on Modal serverless infrastructure.

        Requires MODAL_TOKEN_ID and MODAL_TOKEN_SECRET.
        """
        import json
        import urllib.request
        import urllib.error

        started = time.monotonic()
        try:
            import modal
            app = modal.App.lookup("forge-compute", create_if_missing=True)
            # Use Modal's sandbox for code execution
            with modal.Sandbox.create(
                    app=app, timeout=int(timeout)) as sandbox:
                exec = sandbox.exec("python3", "-I", "-c", code)
                stdout = exec.stdout.read()
                stderr = exec.stderr.read()
                exit_code = exec.wait()
            elapsed = time.monotonic() - started
            output = (stdout or "") + (stderr or "")
            return {
                "status": "succeeded" if exit_code == 0 else "failed",
                "output": output[:MAX_OUTPUT],
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
        except Exception as exc:
            elapsed = time.monotonic() - started
            return {
                "status": "failed",
                "output": f"Modal error: {exc}",
                "output_truncated": False,
                "elapsed_ms": round(elapsed * 1000, 1),
                "backend": "modal",
                "timed_out": False,
            }
