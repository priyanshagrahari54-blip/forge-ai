"""Background worker-liveness maintenance for the Forge control plane."""
from __future__ import annotations

import threading
import time
from typing import Optional

from forge.workers.registry import WorkerRegistry


class WorkerHeartbeatService:
    """Reaps stale workers without granting or renewing execution authority."""

    def __init__(self, registry: WorkerRegistry, *, interval: float = 15.0) -> None:
        self.registry = registry
        self.interval = max(1.0, float(interval))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_reaped = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="forge-worker-heartbeats", daemon=True)
        self._thread.start()

    def stop(self, wait: bool = True) -> None:
        self._stop.set()
        thread = self._thread
        if wait and thread is not None and thread.is_alive():
            thread.join(timeout=max(2.0, self.interval + 1.0))
        self._thread = None

    def tick(self) -> int:
        self.last_reaped = self.registry.reap(time.time())
        return self.last_reaped

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.tick()
