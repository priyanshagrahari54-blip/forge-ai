"""Cooperative run control for supervised autonomous runs (A34).

:class:`SupervisorControl` lets an external operator (e.g. the browser
cockpit via the control plane) pause or cancel a running
:meth:`Supervisor.run <forge.core.supervisor.Supervisor.run>` without
threads, signals, or forceful termination. The supervisor calls
:meth:`checkpoint` at stage boundaries; a checkpoint raises
:class:`TaskCancelled` when cancellation was requested and blocks while a
pause is in effect.

Cancellation is cooperative and safe: the supervisor's existing ``except``
path performs its normal candidate-scoped rollback, so a cancelled run
leaves no half-applied change set behind. With no control object attached
(``None``, the default everywhere) behavior is byte-identical to A32.
"""
from __future__ import annotations

import threading
from typing import Callable


class TaskCancelled(Exception):
    """Raised inside a supervised run when the operator cancelled it."""


class SupervisorControl:
    """Thread-safe pause/cancel flags for one supervised run."""

    def __init__(self) -> None:
        self._cancel = threading.Event()
        self._pause = threading.Event()
        self._pause_ack = threading.Event()
        self._lock = threading.Lock()
        self._on_pause: Callable[[bool], None] | None = None

    def request_cancel(self) -> None:
        """Request cancellation; the run stops at the next checkpoint."""
        self._cancel.set()
        # A paused run must wake up so it can observe the cancellation.
        self._pause.clear()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def request_pause(self) -> None:
        """Request a pause; the run blocks at the next checkpoint."""
        if not self._cancel.is_set():
            self._pause.set()

    def request_resume(self) -> None:
        """Clear a pause request so a paused run continues."""
        self._pause.clear()

    @property
    def pause_requested(self) -> bool:
        return self._pause.is_set()

    @property
    def paused(self) -> bool:
        """True while the run is actually blocked inside a checkpoint."""
        return self._pause_ack.is_set()

    def on_pause_state(self, callback: Callable[[bool], None] | None) -> None:
        """Register ``callback(paused)`` for pause begin/end transitions."""
        with self._lock:
            self._on_pause = callback

    def _notify(self, paused: bool) -> None:
        with self._lock:
            callback = self._on_pause
        if callback is not None:
            try:
                callback(paused)
            except Exception:
                # Observability must never break the enforcement path.
                pass

    def checkpoint(self, stage: str = "") -> None:
        """Yield to operator control. Raises :class:`TaskCancelled`."""
        del stage  # stage names are for callers/logs, not for control state.
        if self._cancel.is_set():
            raise TaskCancelled("cancelled by operator")
        if not self._pause.is_set():
            return
        self._pause_ack.set()
        self._notify(True)
        try:
            while self._pause.is_set() and not self._cancel.is_set():
                # Short waits keep cancellation responsive while paused.
                self._pause.wait(timeout=0.1)
        finally:
            self._pause_ack.clear()
            self._notify(False)
        if self._cancel.is_set():
            raise TaskCancelled("cancelled by operator")
