"""Single admission boundary for local/remote Forge execution.

This module intentionally separates *admission* from *transport*. A worker
registry record is not proof that a remote machine is reachable or trusted;
the configured remote backend remains responsible for authenticated,
allowlisted transport and its own security policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from forge.security.sandbox_policy import SandboxPolicy
from forge.workers.admission import Admission, AdmissionError, WorkerAdmission, WorkerRequirements


@dataclass
class ExecutionReceipt:
    mode: str
    status: str
    backend: str
    worker_id: str = ""
    worker_name: str = ""
    admission: Optional[Admission] = None
    result: Optional[dict[str, Any]] = None
    error: str = ""

    @property
    def ok(self) -> bool:
        """True only for a transport that reported a successful status."""
        return self.status in ("succeeded", "success", "ok", "completed")

    def to_dict(self) -> dict[str, Any]:
        payload = self._to_dict()
        payload["ok"] = self.ok
        return payload

    def _to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "status": self.status,
            "backend": self.backend,
            "worker_id": self.worker_id,
            "worker_name": self.worker_name,
            "admission": self.admission.to_dict() if self.admission else None,
            "result": self.result,
            "error": self.error,
        }


class ExecutionGateway:
    """Route execution through an explicit local/remote boundary.

    Local execution is delegated to the supplied callable. Remote execution
    requires explicit requirements and worker admission; no registry entry is
    ever treated as a transport implementation.
    """

    def __init__(self, registry: Any, *, heartbeat_ttl: float = 60.0) -> None:
        self.admission = WorkerAdmission(registry, heartbeat_ttl=heartbeat_ttl)

    def local(self, execute: Callable[[], dict[str, Any]], *,
              sandbox: Optional[SandboxPolicy] = None) -> ExecutionReceipt:
        """Execute through a caller-owned local backend.

        The gateway records the policy object for the caller but does not
        pretend a declarative policy is OS-level isolation. Backends must
        enforce limits they advertise.
        """
        try:
            result = execute()
            return ExecutionReceipt(
                mode="local", status=str(result.get("status", "succeeded")),
                backend=str(result.get("backend", "local")), result=result)
        except Exception as exc:
            return ExecutionReceipt(mode="local", status="failed",
                                    backend="local", error=str(exc))

    def remote(self, requirements: WorkerRequirements,
               execute: Callable[[Admission], dict[str, Any]]) -> ExecutionReceipt:
        """Admit one worker, execute through an authenticated transport, release.

        ``execute`` must be the real configured remote transport. This method
        never opens sockets, shells, or SSH sessions itself.
        """
        admission: Optional[Admission] = None
        try:
            admission = self.admission.admit(requirements)
            result = execute(admission)
            status = str(result.get("status", "succeeded"))
            return ExecutionReceipt(mode="remote", status=status,
                                    backend=str(result.get("backend", "remote")),
                                    worker_id=admission.worker_id,
                                    worker_name=admission.worker_name,
                                    admission=admission, result=result)
        except AdmissionError as exc:
            return ExecutionReceipt(mode="remote", status="refused",
                                    backend="remote", error=str(exc))
        except Exception as exc:
            return ExecutionReceipt(
                mode="remote", status="failed", backend="remote",
                worker_id=admission.worker_id if admission else "",
                worker_name=admission.worker_name if admission else "",
                admission=admission, error=str(exc))
        finally:
            if admission is not None:
                self.admission.release(admission)
