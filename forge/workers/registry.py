"""Durable worker registry for Forge remote execution.

The registry is deliberately control-plane metadata only. Registration or a
heartbeat never proves that a worker can execute code; the execution backend
must still perform its own authenticated transport and capability checks.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


@dataclass
class WorkerRecord:
    worker_id: str
    name: str
    capabilities: Tuple[str, ...] = ()
    cpu_threads: int = 1
    ram_mb: int = 0
    gpu: bool = False
    platform: str = "unknown"
    endpoint: str = ""
    registered_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    active_jobs: int = 0
    revoked: bool = False

    def live(self, now: Optional[float] = None, ttl: float = 60.0) -> bool:
        if self.revoked:
            return False
        now = time.time() if now is None else now
        return (now - self.last_heartbeat) <= max(1.0, ttl)

    def to_dict(self, now: Optional[float] = None, ttl: float = 60.0) -> Dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "name": self.name,
            "capabilities": list(self.capabilities),
            "cpu_threads": self.cpu_threads,
            "ram_mb": self.ram_mb,
            "gpu": self.gpu,
            "platform": self.platform,
            "endpoint": self.endpoint,
            "registered_at": self.registered_at,
            "last_heartbeat": self.last_heartbeat,
            "active_jobs": self.active_jobs,
            "revoked": self.revoked,
            "live": self.live(now, ttl),
        }


class WorkerRegistry:
    """Thread-safe in-memory worker admission registry.

    Persistence belongs to the server/database layer. This class provides
    deterministic admission and liveness semantics for one control-plane
    process and is safe to use concurrently from worker API handlers.
    """

    def __init__(self, *, heartbeat_ttl: float = 60.0) -> None:
        self.heartbeat_ttl = max(1.0, float(heartbeat_ttl))
        self._workers: Dict[str, WorkerRecord] = {}
        self._lock = threading.RLock()

    def register(self, *, name: str, capabilities: Tuple[str, ...] = (),
                 cpu_threads: int = 1, ram_mb: int = 0, gpu: bool = False,
                 platform: str = "unknown", endpoint: str = "") -> WorkerRecord:
        if not name or len(name) > 200:
            raise ValueError("worker name is required and must be <= 200 chars")
        if cpu_threads < 1 or ram_mb < 0:
            raise ValueError("invalid worker resources")
        worker_id = uuid.uuid4().hex
        now = time.time()
        record = WorkerRecord(
            worker_id=worker_id,
            name=name.strip(),
            capabilities=tuple(sorted(set(str(x).strip() for x in capabilities if str(x).strip()))),
            cpu_threads=int(cpu_threads), ram_mb=int(ram_mb), gpu=bool(gpu),
            platform=str(platform).strip()[:100] or "unknown",
            endpoint=str(endpoint).strip()[:500],
            registered_at=now, last_heartbeat=now,
        )
        with self._lock:
            self._workers[worker_id] = record
        return record

    def heartbeat(self, worker_id: str) -> bool:
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None or worker.revoked:
                return False
            worker.last_heartbeat = time.time()
            return True

    def revoke(self, worker_id: str) -> bool:
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                return False
            worker.revoked = True
            return True

    def get(self, worker_id: str) -> Optional[WorkerRecord]:
        with self._lock:
            return self._workers.get(worker_id)

    def select(self, *, required_capabilities: Tuple[str, ...] = (),
               min_ram_mb: int = 0, min_cpu_threads: int = 1,
               require_gpu: bool = False, platform: str = "",
               now: Optional[float] = None) -> Optional[WorkerRecord]:
        required = set(required_capabilities)
        with self._lock:
            candidates = []
            for worker in self._workers.values():
                if not worker.live(now, self.heartbeat_ttl):
                    continue
                if worker.ram_mb < min_ram_mb or worker.cpu_threads < min_cpu_threads:
                    continue
                if require_gpu and not worker.gpu:
                    continue
                if platform and worker.platform.lower() != platform.lower():
                    continue
                if not required.issubset(set(worker.capabilities)):
                    continue
                candidates.append(worker)
            if not candidates:
                return None
            return min(candidates, key=lambda w: (w.active_jobs, -w.cpu_threads, w.worker_id))

    def begin_job(self, worker_id: str) -> bool:
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None or not worker.live(self.heartbeat_ttl):
                return False
            worker.active_jobs += 1
            return True

    def end_job(self, worker_id: str) -> bool:
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                return False
            worker.active_jobs = max(0, worker.active_jobs - 1)
            return True

    def reap(self, now: Optional[float] = None) -> int:
        """Mark stale workers revoked; never silently delete their metadata."""
        now = time.time() if now is None else now
        count = 0
        with self._lock:
            for worker in self._workers.values():
                if not worker.revoked and not worker.live(now, self.heartbeat_ttl):
                    worker.revoked = True
                    count += 1
        return count

    def snapshot(self, now: Optional[float] = None) -> Dict[str, Any]:
        with self._lock:
            workers = [w.to_dict(now, self.heartbeat_ttl) for w in self._workers.values()]
        return {
            "schema_version": 1,
            "heartbeat_ttl_seconds": self.heartbeat_ttl,
            "worker_count": len(workers),
            "live_count": sum(1 for w in workers if w["live"]),
            "workers": workers,
        }
