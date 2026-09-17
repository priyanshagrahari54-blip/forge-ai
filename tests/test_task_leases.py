import sqlite3
import time
from pathlib import Path

import pytest

from forge.core.task_engine import TaskStatus
from forge.core.task_queue import PersistentTaskQueue
from forge.core.task_store import TaskStore


def test_lease_heartbeat_is_created_and_owner_can_renew(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    queue = PersistentTaskQueue(store=store)
    queue.add("task-1", "long running work")

    claimed = queue.start_next()
    assert claimed is not None
    assert claimed.lease_id
    assert claimed.lease_heartbeat > 0

    renewed = queue.renew_lease("task-1", claimed.lease_id)
    assert renewed is not None
    assert renewed.lease_id == claimed.lease_id
    assert renewed.lease_heartbeat >= claimed.lease_heartbeat


def test_stale_worker_cannot_renew_or_complete_after_recovery(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    queue = PersistentTaskQueue(store=store)
    queue.add("task-1", "work")
    claimed = queue.start_next()
    assert claimed is not None

    recovered = queue.recover_stale_running(0.001)
    assert [task.id for task in recovered] == ["task-1"]
    assert queue.renew_lease("task-1", claimed.lease_id) is None
    assert queue.complete_if_owner("task-1", claimed.lease_id) is None


def test_fresh_lease_is_not_recovered(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    queue = PersistentTaskQueue(store=store)
    queue.add("task-1", "work")
    claimed = queue.start_next()
    assert claimed is not None

    recovered = queue.recover_stale_running(60.0)
    assert recovered == []
    assert TaskStore(tmp_path / "tasks.db").load("task-1").status == TaskStatus.RUNNING


def test_expired_recovery_is_atomic_against_competing_recovery(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    store = TaskStore(path)
    queue = PersistentTaskQueue(store=store)
    queue.add("task-1", "work")
    claimed = queue.start_next()
    assert claimed is not None

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE tasks SET lease_heartbeat = ? WHERE id = ?",
            (time.time() - 60, "task-1"),
        )

    first = TaskStore(path).recover_stale_running(1.0)
    second = TaskStore(path).recover_stale_running(1.0)
    assert [task.id for task in first] == ["task-1"]
    assert second == []
    assert TaskStore(path).load("task-1").status == TaskStatus.RECOVERY


def test_invalid_lease_timeout_is_rejected(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    with pytest.raises(ValueError):
        store.recover_stale_running(0)
