"""Capability-aware remote compute worker registry.

This module is deliberately transport-neutral: it does not open SSH sockets,
start containers, or claim that a worker is reachable merely because it was
registered.  A worker becomes eligible only while its heartbeat is fresh and
its advertised capabilities satisfy the request.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class WorkerCapability:
    """Static worker capabilities used for scheduling admission."""

    cpu_threads: int = 1
    memory_mb: int = 0
    gpu: bool = False
    platforms: Tuple[str, ...] = ()
    labels: Tuple[str, ...] = ()


@dataclass
class ComputeWorker:
    """A remotely registered compute worker and its liveness evidence."""

    worker_id: str
    capability: WorkerCapability
    endpoint: str = ""
    state: str = "REGISTERED"
    last_heartbeat: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    active_jobs: int = 0

    def fresh(self, now: Optional[float] = None, ttl: float = 30.0) -> bool:
        now = time.time() if now is None else now
        return self.last_heartbeat > 0 and now - self.last_heartbeat <= ttl

    def to_dict(self, *, now: Optional[float] = None, ttl: float = 30.0) -> Dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "endpoint": self.endpoint,
            "state": self.state,
            "last_heartbeat": self.last_heartbeat,
            "fresh": self.fresh(now=now, ttl=ttl),
            "active_jobs": self.active_jobs,
            "capability": {
                "cpu_threads": self.capability.cpu_threads,
                "memory_mb": self.capability.memory_mb,
                "gpu": self.capability.gpu,
                "platforms": list(self.capability.platforms),
                "labels": list(self.capability.labels),
            },
            "metadata": dict(self.metadata),
        }


class WorkerRegistry:
    """Thread-safe registry with fail-closed worker selection."""

    def __init__(self, *, heartbeat_ttl: float = 30.0) -> None:
        if heartbeat_ttl <= 0:
            raise ValueError("heartbeat_ttl must be greater than zero")
        self.heartbeat_ttl = heartbeat_ttl
        self._workers: Dict[str, ComputeWorker] = {}
        self._lock = threading.RLock()

    def register(self, worker: ComputeWorker) -> ComputeWorker:
        if not worker.worker_id or len(worker.worker_id) > 128:
            raise ValueError("worker_id is required and must be <=128 characters")
        with self._lock:
            worker.state = "ONLINE"
            worker.last_heartbeat = time.time()
            self._workers[worker.worker_id] = worker
            return worker

    def heartbeat(self, worker_id: str) -> Optional[ComputeWorker]:
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None or worker.state == "REVOKED":
                return None
            worker.last_heartbeat = time.time()
            worker.state = "ONLINE"
            return worker

    def revoke(self, worker_id: str) -> bool:
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                return False
            worker.state = "REVOKED"
            return True

    def mark_stale(self, *, now: Optional[float] = None) -> List[ComputeWorker]:
        now = time.time() if now is None else now
        stale: List[ComputeWorker] = []
        with self._lock:
            for worker in self._workers.values():
                if worker.state == "ONLINE" and not worker.fresh(now, self.heartbeat_ttl):
                    worker.state = "STALE"
                    stale.append(worker)
        return stale

    def get(self, worker_id: str) -> Optional[ComputeWorker]:
        with self._lock:
            return self._workers.get(worker_id)

    def select(
        self,
        *,
        min_memory_mb: int = 0,
        min_cpu_threads: int = 1,
        gpu: bool = False,
        platform: str = "",
        labels: Iterable[str] = (),
        now: Optional[float] = None,
    ) -> Optional[ComputeWorker]:
        """Select the least-loaded fresh worker satisfying every requirement."""
        wanted_labels = set(labels)
        now = time.time() if now is None else now
        with self._lock:
            candidates: List[ComputeWorker] = []
            for worker in self._workers.values():
                capability = worker.capability
                if worker.state != "ONLINE" or not worker.fresh(now, self.heartbeat_ttl):
                    continue
                if capability.memory_mb < min_memory_mb:
                    continue
                if capability.cpu_threads < min_cpu_threads:
                    continue
                if gpu and not capability.gpu:
                    continue
                if platform and platform not in capability.platforms:
                    continue
                if not wanted_labels.issubset(set(capability.labels)):
                    continue
                candidates.append(worker)
            if not candidates:
                return None
            return min(candidates, key=lambda item: (item.active_jobs, item.worker_id))

    def snapshot(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        now = time.time() if now is None else now
        with self._lock:
            workers = [
                worker.to_dict(now=now, ttl=self.heartbeat_ttl)
                for worker in sorted(self._workers.values(), key=lambda item: item.worker_id)
            ]
        return {
            "heartbeat_ttl": self.heartbeat_ttl,
            "worker_count": len(workers),
            "online_count": sum(1 for worker in workers if worker["state"] == "ONLINE" and worker["fresh"]),
            "workers": workers,
        }
