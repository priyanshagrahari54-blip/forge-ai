from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from forge.core.task_engine import Task, TaskStatus
from forge.core.task_queue import PersistentTaskQueue
from forge.core.task_recovery import TaskRecoveryEngine


@dataclass(frozen=True)
class WorkerLease:
    """A worker's lease over a running task."""

    task_id: str
    worker_id: str
    leased_at: datetime
    heartbeat_at: datetime


class PersistentWorkerRuntime:
    """Lease-based worker runtime over the persistent task queue.

    Tasks are persisted as RUNNING while leased. If a worker stops
    heartbeating, the runtime moves the task to RECOVERY via the
    configured TaskRecoveryEngine.
    """

    def __init__(
        self,
        queue: PersistentTaskQueue,
        recovery: TaskRecoveryEngine,
        *,
        lease_timeout: timedelta = timedelta(minutes=5),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.queue = queue
        self.recovery = recovery
        self.lease_timeout = lease_timeout
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._leases: dict[str, WorkerLease] = {}

    def claim_next(self, worker_id: str) -> Task | None:
        """Claim the highest-priority ready task for a worker."""
        started = self.queue.start_next()
        if started is None:
            return None

        current = self.now()
        self._leases[started.id] = WorkerLease(
            task_id=started.id,
            worker_id=worker_id,
            leased_at=current,
            heartbeat_at=current,
        )
        return started

    def heartbeat(self, task_id: str, worker_id: str) -> bool:
        """Refresh the heartbeat timestamp for an active lease."""
        lease = self._leases.get(task_id)
        if lease is None or lease.worker_id != worker_id:
            return False

        self._leases[task_id] = WorkerLease(
            task_id=lease.task_id,
            worker_id=lease.worker_id,
            leased_at=lease.leased_at,
            heartbeat_at=self.now(),
        )
        return True

    def complete(self, task_id: str, worker_id: str) -> Task:
        """Complete a leased task and release its worker lease."""
        self._require_owner(task_id, worker_id)
        try:
            return self.queue.complete(task_id)
        finally:
            self._leases.pop(task_id, None)

    def fail(self, task_id: str, worker_id: str, error: str) -> Task:
        """Fail a leased task and release its worker lease."""
        self._require_owner(task_id, worker_id)
        try:
            return self.queue.fail(task_id, error)
        finally:
            self._leases.pop(task_id, None)

    def release(self, task_id: str, worker_id: str, reason: str) -> Task:
        """Release a leased task into recovery without marking failure."""
        self._require_owner(task_id, worker_id)
        task = self.queue.engine._find(task_id)
        try:
            return self.recovery.mark_recovery(task, reason)
        finally:
            self._leases.pop(task_id, None)

    def recover_stale_leases(self) -> list[Task]:
        """Move stale leased tasks to recovery and release leases."""
        threshold = self.now() - self.lease_timeout
        stale_ids = [
            task_id
            for task_id, lease in self._leases.items()
            if lease.heartbeat_at < threshold
        ]

        recovered: list[Task] = []
        for task_id in stale_ids:
            lease = self._leases.pop(task_id)
            task = self.queue.engine._find(task_id)
            if task.status == TaskStatus.RUNNING:
                recovered.append(
                    self.recovery.mark_recovery(
                        task,
                        (
                            f"Worker lease expired for {lease.worker_id}; "
                            "task moved to recovery."
                        ),
                    )
                )

        return recovered

    def active_leases(self) -> tuple[WorkerLease, ...]:
        """Return active leases sorted deterministically by task id."""
        return tuple(
            self._leases[task_id]
            for task_id in sorted(self._leases)
        )

    def owner_of(self, task_id: str) -> str:
        lease = self._leases.get(task_id)
        return "" if lease is None else lease.worker_id

    def _require_owner(self, task_id: str, worker_id: str) -> None:
        lease = self._leases.get(task_id)
        if lease is None:
            raise RuntimeError(f"Task is not leased: {task_id}")
        if lease.worker_id != worker_id:
            raise RuntimeError(
                f"Task {task_id} is leased by {lease.worker_id}, not {worker_id}"
            )
