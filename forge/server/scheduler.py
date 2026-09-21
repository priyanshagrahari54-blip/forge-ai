"""The scheduler: leases queued work to background workers (A81).

One dispatcher thread turns the persistent queue into running tasks. It also
owns lightweight runtime verification housekeeping so model health continues
without a browser/client connection.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from forge.server.models import TaskStatus

DEFAULT_POLL_INTERVAL = 0.25
HOUSEKEEPING_INTERVAL = 30.0


class Scheduler:
    """Dispatch loop: queue leases → worker pool submissions."""

    def __init__(self, server: Any,
                 *, poll_interval: float = DEFAULT_POLL_INTERVAL) -> None:
        self.server = server
        self.poll_interval = max(0.05, float(poll_interval))
        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._active: Dict[str, int] = {}
        self._active_lock = threading.Lock()
        self._last_housekeeping = 0.0
        from forge.models.runtime_monitor_service import RuntimeMonitorService
        runtime_root = Path(server.config.db_path).parent.parent
        interval = _env_float("FORGE_RUNTIME_MONITOR_INTERVAL", 60.0)
        ttl = _env_float("FORGE_RUNTIME_VERIFICATION_TTL", 300.0)
        self.runtime_monitor = RuntimeMonitorService(
            server.fabric,
            state_path=runtime_root / "runtime-monitor.json",
            interval_seconds=interval,
            verification_ttl_seconds=ttl,
            inference_probes=getattr(
                server.config, "runtime_inference_probes", None),
        )

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

    # -- accounting ------------------------------------------------------------

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
                pass
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
        try:
            self.runtime_monitor.tick(now=now)
        except Exception:
            # Runtime verification is fail-closed and must never kill task
            # dispatch. Detailed state is persisted by the service itself.
            pass

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
                break
            owner = "%s:worker:%s" % (server.boot_id, uuid4().hex[:8])
            task_id = server.queue.lease_next(project_id, owner)
            if task_id is None:
                continue
            task = server.tasks.get(task_id)
            if task is None or task.terminal:
                server.queue.release(task_id)
                continue
            if task.status != TaskStatus.QUEUED:
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


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
