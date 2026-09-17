"""Background worker-lease supervision for persistent Forge tasks.

The monitor is deliberately independent from the control plane so it can be
embedded by servers, workers, or tests without changing task semantics.  It
never completes or fails a task itself: lease renewal is owner-authenticated
and stale recovery is an atomic store operation.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from forge.core.task_queue import PersistentTaskQueue
from forge.core.task_engine import Task


class LeaseMonitor:
    """Renew an owned lease and recover genuinely stale workers."""

    def __init__(
        self,
        queue: PersistentTaskQueue,
        *,
        task_id: str,
        lease_id: str,
        heartbeat_interval: float = 10.0,
        stale_after: float = 45.0,
        on_stale: Optional[Callable[[Task], None]] = None,
    ) -> None:
        if heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be greater than zero")
        if stale_after <= heartbeat_interval:
            raise ValueError("stale_after must be greater than heartbeat_interval")
        if not task_id or not lease_id:
            raise ValueError("task_id and lease_id are required")
        self.queue = queue
        self.task_id = task_id
        self.lease_id = lease_id
        self.heartbeat_interval = heartbeat_interval
        self.stale_after = stale_after
        self.on_stale = on_stale
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lost = threading.Event()

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def tick(self) -> Optional[Task]:
        """Renew this worker's lease once, or mark ownership as lost."""
        task = self.queue.renew_lease(self.task_id, self.lease_id)
        if task is None:
            self._lost.set()
        return task

    def recover_stale(self) -> list[Task]:
        """Recover leases whose persisted heartbeat has actually expired."""
        recovered = self.queue.recover_stale_running(self.stale_after)
        if self.on_stale is not None:
            for task in recovered:
                self.on_stale(task)
        return recovered

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="forge-lease-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, wait: bool = True) -> None:
        self._stop.set()
        if wait and self._thread is not None:
            self._thread.join(timeout=max(1.0, self.heartbeat_interval + 1.0))
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.heartbeat_interval):
            if self.tick() is None:
                return
            self.recover_stale()
