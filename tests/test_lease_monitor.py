from __future__ import annotations

import time

from forge.core.task_engine import TaskStatus
from forge.core.task_queue import PersistentTaskQueue
from forge.core.task_store import TaskStore
from forge.core.lease_monitor import LeaseMonitor


def _queue(tmp_path):
    return PersistentTaskQueue(store=TaskStore(tmp_path / "tasks.db"))


def test_monitor_renews_only_current_owner(tmp_path):
    queue = _queue(tmp_path)
    queue.add("t1", "long task")
    claimed = queue.start_next()
    assert claimed is not None

    monitor = LeaseMonitor(
        queue, task_id="t1", lease_id=claimed.lease_id,
        heartbeat_interval=0.01, stale_after=0.05,
    )
    before = queue.store.load("t1").lease_heartbeat
    time.sleep(0.002)
    assert monitor.tick() is not None
    after = queue.store.load("t1").lease_heartbeat
    assert after >= before
    assert monitor.lost is False

    assert queue.renew_lease("t1", "stale-worker") is None
    assert queue.store.load("t1").status == TaskStatus.RUNNING


def test_monitor_recovers_expired_lease_and_notifies(tmp_path):
    queue = _queue(tmp_path)
    queue.add("t1", "stale task")
    claimed = queue.start_next()
    assert claimed is not None

    # Force an old heartbeat in the durable store to model a dead worker.
    with queue.store._connect() as connection:
        connection.execute(
            "UPDATE tasks SET lease_heartbeat = ? WHERE id = ?",
            (time.time() - 100, "t1"),
        )

    seen = []
    monitor = LeaseMonitor(
        queue, task_id="t1", lease_id=claimed.lease_id,
        heartbeat_interval=0.01, stale_after=1.0,
        on_stale=seen.append,
    )
    recovered = monitor.recover_stale()
    assert [task.id for task in recovered] == ["t1"]
    assert [task.id for task in seen] == ["t1"]
    assert queue.store.load("t1").status == TaskStatus.RECOVERY


def test_monitor_rejects_invalid_timing(tmp_path):
    queue = _queue(tmp_path)
    try:
        LeaseMonitor(queue, task_id="t1", lease_id="l1", heartbeat_interval=2, stale_after=1)
    except ValueError:
        pass
    else:
        raise AssertionError("expected stale_after validation")
