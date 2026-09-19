"""Safe job admission across local and registered remote workers.

Admission is intentionally separate from execution. A worker can only be
selected when it is live, capable, and resource-compatible. This module never
opens a shell, connects to an endpoint, or treats registration as proof of
execution authority.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from forge.workers.registry import WorkerRecord, WorkerRegistry


@dataclass(frozen=True)
class WorkerRequirements:
    """Declarative requirements extracted from a task policy."""

    capabilities: Tuple[str, ...] = ()
    min_ram_mb: int = 0
    min_cpu_threads: int = 1
    require_gpu: bool = False
    platform: str = ""
    remote_required: bool = False
    retryable: bool = False
    idempotent: bool = False
    max_runtime_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.min_ram_mb < 0 or self.min_cpu_threads < 1:
            raise ValueError("invalid worker resource requirements")
        if self.max_runtime_seconds <= 0:
            raise ValueError("max_runtime_seconds must be positive")

    def to_dict(self) -> Dict[str, object]:
        return {
            "capabilities": list(self.capabilities),
            "min_ram_mb": self.min_ram_mb,
            "min_cpu_threads": self.min_cpu_threads,
            "require_gpu": self.require_gpu,
            "platform": self.platform,
            "remote_required": self.remote_required,
            "retryable": self.retryable,
            "idempotent": self.idempotent,
            "max_runtime_seconds": self.max_runtime_seconds,
        }


@dataclass(frozen=True)
class Admission:
    """An admitted execution reservation."""

    worker_id: str
    worker_name: str
    requirements: WorkerRequirements
    remote: bool = True

    def to_dict(self) -> Dict[str, object]:
        return {
            "worker_id": self.worker_id,
            "worker_name": self.worker_name,
            "remote": self.remote,
            "requirements": self.requirements.to_dict(),
        }


class AdmissionError(RuntimeError):
    """Fail-closed admission error."""


class WorkerAdmission:
    """Atomically reserve a suitable live worker for a remote job."""

    def __init__(self, registry: WorkerRegistry,
                 *, heartbeat_ttl: Optional[float] = None) -> None:
        self.registry = registry
        #: Optional stricter/looser liveness window for admission. The
        #: registry is the single source of truth for liveness, so an
        #: override is applied there instead of being silently ignored.
        if heartbeat_ttl is not None and hasattr(registry, "heartbeat_ttl"):
            registry.heartbeat_ttl = max(1.0, float(heartbeat_ttl))

    def admit(self, requirements: WorkerRequirements,
              *, now: Optional[float] = None) -> Admission:
        if not requirements.remote_required:
            raise AdmissionError(
                "remote admission requested without remote_required=True")
        worker = self.registry.select(
            required_capabilities=requirements.capabilities,
            min_ram_mb=requirements.min_ram_mb,
            min_cpu_threads=requirements.min_cpu_threads,
            require_gpu=requirements.require_gpu,
            platform=requirements.platform,
            now=now,
        )
        if worker is None:
            raise AdmissionError("no live worker satisfies the task requirements")
        if not self.registry.begin_job(worker.worker_id):
            raise AdmissionError("selected worker became unavailable before reservation")
        return Admission(
            worker_id=worker.worker_id,
            worker_name=worker.name,
            requirements=requirements,
        )

    def release(self, admission: Admission) -> None:
        self.registry.end_job(admission.worker_id)

    def can_retry_after_expiry(self, requirements: WorkerRequirements) -> bool:
        """Expired leases are retryable only for explicitly safe tasks."""
        return bool(requirements.retryable and requirements.idempotent)
