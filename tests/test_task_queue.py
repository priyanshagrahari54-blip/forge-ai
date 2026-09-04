from __future__ import annotations

from forge.core.task_engine import TaskStatus
from forge.core.task_queue import PersistentTaskQueue
from forge.core.task_store import TaskStore


def test_add_persists_task(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    queue = PersistentTaskQueue(store=store)

    queue.add("task-1", "Build feature")

    restarted = PersistentTaskQueue(store=TaskStore(tmp_path / "tasks.db"))
    tasks = restarted.load()

    assert len(tasks) == 1
    assert tasks[0].id == "task-1"
    assert tasks[0].description == "Build feature"


def test_ready_returns_tasks_with_completed_dependencies(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    queue = PersistentTaskQueue(store=store)

    queue.add("setup", "Setup")
    queue.add(
        "code",
        "Code",
        dependencies=["setup"],
    )

    assert [task.id for task in queue.ready()] == ["setup"]

    queue.start_next()
    queue.complete("setup")

    assert [task.id for task in queue.ready()] == ["code"]


def test_next_uses_priority(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    queue = PersistentTaskQueue(store=store)

    queue.add("low", "Low priority", priority=1)
    queue.add("high", "High priority", priority=10)

    assert queue.next() is not None
    assert queue.next().id == "high"


def test_start_next_persists_running_state(tmp_path) -> None:
    database = tmp_path / "tasks.db"

    queue = PersistentTaskQueue(
        store=TaskStore(database)
    )
    queue.add("task-1", "Build")

    started = queue.start_next()

    assert started is not None
    assert started.status == TaskStatus.RUNNING

    restarted = PersistentTaskQueue(
        store=TaskStore(database)
    )
    tasks = restarted.load()

    assert tasks[0].status == TaskStatus.RUNNING
    assert tasks[0].attempts == 1


def test_complete_persists_state(tmp_path) -> None:
    database = tmp_path / "tasks.db"

    queue = PersistentTaskQueue(
        store=TaskStore(database)
    )
    queue.add("task-1", "Build")
    queue.start_next()
    queue.complete("task-1")

    restarted = PersistentTaskQueue(
        store=TaskStore(database)
    )
    tasks = restarted.load()

    assert tasks[0].status == TaskStatus.COMPLETED


def test_failed_task_is_persisted(tmp_path) -> None:
    database = tmp_path / "tasks.db"

    queue = PersistentTaskQueue(
        store=TaskStore(database)
    )
    queue.add("task-1", "Build")
    queue.start_next()
    queue.fail("task-1", "Build failed")

    restarted = PersistentTaskQueue(
        store=TaskStore(database)
    )
    tasks = restarted.load()

    assert tasks[0].status == TaskStatus.FAILED
    assert tasks[0].errors == ["Build failed"]


def test_pending_returns_only_pending_tasks(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    queue = PersistentTaskQueue(store=store)

    queue.add("pending", "Pending")
    queue.add("running", "Running")

    queue.start_next()

    assert [task.id for task in queue.pending()] == ["running"]


def test_multiple_ready_tasks_are_deterministic(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    queue = PersistentTaskQueue(store=store)

    queue.add("a", "A", priority=5)
    queue.add("b", "B", priority=5)
    queue.add("c", "C", priority=5)

    assert [task.id for task in queue.ready()] == [
        "a",
        "b",
        "c",
    ]
