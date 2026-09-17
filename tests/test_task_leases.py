from pathlib import Path

from forge.core.task_engine import TaskStatus
from forge.core.task_queue import PersistentTaskQueue
from forge.core.task_store import TaskStore


def test_claim_issues_unique_lease(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    first = PersistentTaskQueue(store=TaskStore(path))
    first.add("task-1", "work")
    claimed = first.start_next()

    assert claimed is not None
    assert claimed.status == TaskStatus.RUNNING
    assert claimed.lease_id

    second = TaskStore(path)
    stored = second.load("task-1")
    assert stored.lease_id == claimed.lease_id


def test_stale_worker_cannot_complete_or_fail(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    queue = PersistentTaskQueue(store=TaskStore(path))
    queue.add("task-1", "work")
    claimed = queue.start_next()
    assert claimed is not None

    assert queue.complete_if_owner("task-1", "stale-lease") is None
    assert queue.fail_if_owner("task-1", "stale-lease", "late failure") is None
    assert TaskStore(path).load("task-1").status == TaskStatus.RUNNING


def test_current_worker_can_complete_with_lease(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    queue = PersistentTaskQueue(store=TaskStore(path))
    queue.add("task-1", "work")
    claimed = queue.start_next()
    assert claimed is not None

    completed = queue.complete_if_owner("task-1", claimed.lease_id)
    assert completed is not None
    assert completed.status == TaskStatus.COMPLETED
    assert completed.lease_id == ""
