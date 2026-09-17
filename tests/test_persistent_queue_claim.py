from pathlib import Path

from forge.core.task_queue import PersistentTaskQueue
from forge.core.task_store import TaskStore


def test_persistent_queue_claim_is_single_owner(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")

    writer = PersistentTaskQueue(store=store)
    writer.add("task-1", "run once")

    worker_a = PersistentTaskQueue(store=TaskStore(tmp_path / "tasks.db"))
    worker_b = PersistentTaskQueue(store=TaskStore(tmp_path / "tasks.db"))
    worker_a.load()
    worker_b.load()

    claimed_a = worker_a.start_next()
    claimed_b = worker_b.start_next()

    assert (claimed_a is None) != (claimed_b is None)

    winner = claimed_a or claimed_b
    assert winner.id == "task-1"
    assert winner.attempts == 1
    assert TaskStore(tmp_path / "tasks.db").load("task-1").attempts == 1


def test_start_next_tries_another_candidate_after_claim_race(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    queue = PersistentTaskQueue(store=store)
    queue.add("task-a", "first", priority=10)
    queue.add("task-b", "second", priority=1)

    other_worker = PersistentTaskQueue(store=TaskStore(tmp_path / "tasks.db"))
    other_worker.load()
    first = other_worker.start_next()
    assert first is not None
    assert first.id == "task-a"

    # This worker still has a stale in-memory view where task-a is pending.
    # start_next must skip the lost claim and atomically claim task-b.
    queue.load()
    claimed = queue.start_next()

    assert claimed is not None
    assert claimed.id == "task-b"
    assert claimed.attempts == 1
