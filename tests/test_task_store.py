from __future__ import annotations

from forge.core.task_engine import Task, TaskStatus
from forge.core.task_store import TaskStore


def test_save_and_load_task(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")

    task = Task(
        id="task-1",
        description="Build feature",
        status=TaskStatus.CODING,
        attempts=2,
        errors=["first failure"],
        dependencies=["setup"],
    )

    store.save(task)

    loaded = store.load("task-1")

    assert loaded == task


def test_save_updates_existing_task(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")

    task = Task(
        id="task-1",
        description="Build feature",
    )

    store.save(task)

    task.status = TaskStatus.COMPLETED
    task.attempts = 1

    store.save(task)

    loaded = store.load("task-1")

    assert loaded.status == TaskStatus.COMPLETED
    assert loaded.attempts == 1


def test_load_all_preserves_insertion_order(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")

    first = Task("first", "First task")
    second = Task("second", "Second task")

    store.save(first)
    store.save(second)

    loaded = store.load_all()

    assert [task.id for task in loaded] == [
        "first",
        "second",
    ]


def test_missing_task_raises_key_error(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")

    try:
        store.load("missing")
    except KeyError as error:
        assert "Task not found" in str(error)
    else:
        raise AssertionError("Expected KeyError")


def test_delete_task(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")

    store.save(Task("task-1", "Build feature"))
    store.delete("task-1")

    assert store.load_all() == []


def test_clear_store(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")

    store.save(Task("task-1", "First"))
    store.save(Task("task-2", "Second"))

    store.clear()

    assert store.load_all() == []


def test_dependencies_and_errors_survive_restart(tmp_path) -> None:
    database = tmp_path / "tasks.db"

    store = TaskStore(database)

    task = Task(
        id="task-1",
        description="Recoverable task",
        status=TaskStatus.RECOVERY,
        attempts=3,
        errors=["timeout", "build failure"],
        dependencies=["prepare", "code"],
    )

    store.save(task)

    restarted_store = TaskStore(database)
    loaded = restarted_store.load("task-1")

    assert loaded.status == TaskStatus.RECOVERY
    assert loaded.attempts == 3
    assert loaded.errors == ["timeout", "build failure"]
    assert loaded.dependencies == ["prepare", "code"]
