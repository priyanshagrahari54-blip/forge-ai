"""Lease-bound worker guard for long-running Forge executions.

The guard keeps worker ownership alive while user work executes.  It is
intentionally execution-agnostic: the caller supplies the work function and
receives a normal return value, while lease loss is surfaced explicitly so a
stale worker cannot silently publish a result.
"""
from __future__ import annotations

import threading
from typing import Callable, Generic, Optional, TypeVar

from forge.core.lease_monitor import LeaseMonitor
from forge.core.task_engine import Task

T = TypeVar("T")


class LeaseLostError(RuntimeError):
    """Raised when a worker loses ownership before publishing its result."""


class LeaseGuard(Generic[T]):
    """Run one callable under a renewable persistent task lease."""

    def __init__(
        self,
        monitor: LeaseMonitor,
        work: Callable[[], T],
    ) -> None:
        self.monitor = monitor
        self.work = work
        self._result: Optional[T] = None
        self._error: Optional[BaseException] = None
        self._done = threading.Event()

    @property
    def result(self) -> T:
        if self._error is not None:
            raise self._error
        if not self._done.is_set():
            raise RuntimeError("LeaseGuard has not completed")
        return self._result  # type: ignore[return-value]

    def run(self) -> T:
        """Execute work and refuse to return a result after lease loss."""
        self.monitor.start()
        try:
            self._result = self.work()
            if self.monitor.lost:
                raise LeaseLostError(
                    f"Worker lease lost for task {self.monitor.task_id}; "
                    "result must not be published by this worker."
                )
            return self._result
        except BaseException as exc:
            self._error = exc
            raise
        finally:
            self._done.set()
            self.monitor.stop(wait=True)

    def recover_stale(self) -> list[Task]:
        """Expose atomic stale recovery for supervisors."""
        return self.monitor.recover_stale()
