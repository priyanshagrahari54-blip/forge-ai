from __future__ import annotations

import sqlite3
import time

import pytest

from forge.core.lease_guard import LeaseGuard, LeaseLostError
from forge.core.lease_monitor import LeaseMonitor
from forge.core.task_engine import TaskStatus
from forge.core.task_queue import PersistentTaskQueue
from forge.core.task_store import TaskStore


def _queue(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    queue = PersistentTaskQueue(store=store)
    queue.add("task-1", "long running work")
    claimed = queue.start_next()
    assert claimed is not None
    return queue, claimed


def test_guard_runs_under_lease(tmp_path):
    queue, claimed = _queue(tmp_path)
    monitor = LeaseMonitor(
        queue,
        task_id=claimed.id,
        lease_id=claimed.lease_id,
        heartbeat_interval=0.01,
        stale_after=0.05,
    )
    guard = LeaseGuard(monitor, lambda: "ok")
    assert guard.run() == "ok"
    assert not monitor.lost


def test_guard_refuses_result_after_ownership_loss(tmp_path):
    queue, claimed = _queue(tmp_path)
    monitor = LeaseMonitor(
        queue,
        task_id=claimed.id,
        lease_id=claimed.lease_id,
        heartbeat_interval=0.01,
        stale_after=0.05,
    )

    # Simulate another worker taking ownership by expiring/recovering the
    # current lease before the guarded worker attempts to publish its result.
    # The heartbeat is backdated rather than slept on: a sleep plus a 1 ms idle
    # window is a wall-clock race that made this test flaky on CI.
    with sqlite3.connect(tmp_path / "tasks.db") as connection:
        connection.execute(
            "UPDATE tasks SET lease_heartbeat = ? WHERE id = ?",
            (time.time() - 60, claimed.id))
    recovered = queue.recover_stale_running(1.0)
    assert recovered and recovered[0].status == TaskStatus.RECOVERY

    guard = LeaseGuard(monitor, lambda: "must-not-publish")
    with pytest.raises(LeaseLostError):
        guard.run()


def test_guard_propagates_work_errors(tmp_path):
    queue, claimed = _queue(tmp_path)
    monitor = LeaseMonitor(
        queue,
        task_id=claimed.id,
        lease_id=claimed.lease_id,
        heartbeat_interval=0.01,
        stale_after=0.05,
    )

    def fail():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        LeaseGuard(monitor, fail).run()
