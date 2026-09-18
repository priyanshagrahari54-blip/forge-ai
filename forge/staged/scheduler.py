"""Persistent five-minute watchdog for Forge staged missions.

The watchdog is deliberately small: it does not duplicate stage execution. It
reconciles persisted autorun intents with live runner threads every five
minutes, so a browser can disappear and a service restart can recover missions.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Optional


DEFAULT_INTERVAL_SECONDS = 300.0
_ATTR = "_forge_stage_scheduler"


class StageScheduler:
    def __init__(self, plane: Any, interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> None:
        self.plane = plane
        self.interval_seconds = max(30.0, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="forge-stage-scheduler",
            daemon=True,
        )
        self._thread.start()

    def tick(self) -> dict:
        from forge.staged.autorun import resume_active
        return resume_active(self.plane)

    def stop(self, wait: bool = True) -> None:
        self._stop.set()
        thread = self._thread
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def _loop(self) -> None:
        # Reconcile once at startup, then every five minutes.
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                pass
            self._stop.wait(self.interval_seconds)


def start_scheduler(plane: Any, interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> StageScheduler:
    scheduler = StageScheduler(plane, interval_seconds=interval_seconds)
    scheduler.start()
    setattr(plane, _ATTR, scheduler)
    return scheduler


def get_scheduler(plane: Any) -> Optional[StageScheduler]:
    return getattr(plane, _ATTR, None)
