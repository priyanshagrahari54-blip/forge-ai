"""The scheduler: leases queued work to background workers (A81).

One dispatcher thread turns the persistent queue into running tasks:

* per-project concurrency is capped (``max_tasks_per_project``), so one
  project can never starve another or double-run a repository;
* global concurrency is capped by the worker pool;
* leases carry the server's ``boot_id``, which is what makes restart
  fencing possible (a lease from a dead boot never validates);
* retry backoff is honored through the queue's ``available_at``;
* housekeeping (session expiry, approval expiry) piggybacks on the
  loop — no extra threads, no timers to leak on Windows.

The scheduler wakes on demand (submit/cancel/resume/finish) and polls
slowly otherwise, so dispatch latency is milliseconds while idle CPU
cost stays near zero.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional
from uuid import uuid4

from forge.server.models import TaskStatus

#: Slow poll when idle; wake() makes dispatch effectively immediate.
DEFAULT_POLL_INTERVAL = 0.25

#: Housekeeping cadence (seconds).
HOUSEKEEPING_INTERVAL = 30.0


class Scheduler:
    """Dispatch loop: queue leases → worker pool submissions."""

    def __init__(self, server: Any, *,
                 poll_interval: float = DEFAULT_POLL_INTERVAL) -> None:
        self.server = server
        self.poll_interval = max(0.05, float(poll_interval))
        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._active: Dict[str, int] = {}
        self._active_lock = threading.Lock()
        self._last_housekeeping = 0.0

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._loop, name="forge-scheduler", daemon=True)
        self._thread.start()

    def stop(self, *, join_timeout: float = 5.0) -> None:
        self._stopping.set()
        self._wake.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=join_timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None

    def wake(self) -> None:
        self._wake.set()

    # -- accounting ----------------------------------------------------------------

    def active_count(self, project_id: str = "") -> int:
        with self._active_lock:
            if project_id:
                return int(self._active.get(project_id, 0))
            return sum(self._active.values())

    def task_finished(self, project_id: str) -> None:
        if not project_id:
            return
        with self._active_lock:
            self._active[project_id] = max(
                0, self._active.get(project_id, 1) - 1)
        self.wake()

    def stats(self) -> Dict[str, Any]:
        with self._active_lock:
            active = dict(self._active)
        return {
            "running": self.running,
            "poll_interval": self.poll_interval,
            "active_per_project": active,
            "active_total": sum(active.values()),
        }

    # -- dispatch ---------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self.dispatch_once()
            except Exception:
                pass  # a bad tick must never kill the dispatcher
            try:
                self._housekeeping()
            except Exception:
                pass
            self._wake.wait(timeout=self.poll_interval)
            self._wake.clear()

    def _housekeeping(self) -> None:
        now = time.time()
        if now - self._last_housekeeping < HOUSEKEEPING_INTERVAL:
            return
        self._last_housekeeping = now
        self.server.sessions.prune()
        self.server.approvals.expire_stale()

    def dispatch_once(self) -> int:
        """One dispatch pass; returns the number of tasks submitted."""
        server = self.server
        if not server.running or not server.pool.alive:
            return 0
        submitted = 0
        for project_id in server.projects.project_ids():
            if self._stopping.is_set():
                break
            config = server.config
            with self._active_lock:
                if (self._active.get(project_id, 0)
                        >= max(1, int(config.max_tasks_per_project))):
                    continue
            if server.pool.busy >= server.pool.max_workers:
                break  # globally saturated; retry on the next tick
            owner = "%s:worker:%s" % (server.boot_id, uuid4().hex[:8])
            task_id = server.queue.lease_next(project_id, owner)
            if task_id is None:
                continue
            task = server.tasks.get(task_id)
            if task is None or task.terminal:
                server.queue.release(task_id)
                continue
            if task.status != TaskStatus.QUEUED:
                # Paused/cancelled after enqueue: give the slot back.
                server.queue.requeue(task_id)
                continue
            with self._active_lock:
                self._active[project_id] = \
                    self._active.get(project_id, 0) + 1
            try:
                from forge.server.workers import run_task

                server.pool.submit(run_task, server, task_id, owner,
                                   project_id)
                submitted += 1
            except Exception as exc:
                with self._active_lock:
                    self._active[project_id] = max(
                        0, self._active.get(project_id, 1) - 1)
                server.queue.requeue(task_id)
                server.log(task_id, project_id,
                           "Dispatch failed: %s" % exc,
                           level="error", source="scheduler")
        return submitted
