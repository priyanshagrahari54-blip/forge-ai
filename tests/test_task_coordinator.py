from __future__ import annotations

from forge.core.task_coordinator import TaskExecutionCoordinator
from forge.core.task_engine import TaskStatus
from forge.core.task_queue import PersistentTaskQueue
from forge.core.task_recovery import TaskRecoveryEngine
from forge.core.task_store import TaskStore


def make_coordinator(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    queue = PersistentTaskQueue(store=store)
    recovery = TaskRecoveryEngine(store)
    return queue, recovery, TaskExecutionCoordinator(queue, recovery)


def test_execute_successfully_completes_task(tmp_path) -> None:
    queue, _, coordinator = make_coordinator(tmp_path)

    queue.add("task-1", "Build project")

    result = coordinator.execute(
        "task-1",
        lambda task: f"completed {task.id}",
    )

    assert result.success
    assert result.output == "completed task-1"
    assert queue.engine._find("task-1").status == TaskStatus.COMPLETED


def test_execute_failure_marks_task_failed(tmp_path) -> None:
    queue, _, coordinator = make_coordinator(tmp_path)

    queue.add("task-1", "Build project")

    def worker(task):
        raise RuntimeError("build failed")

    result = coordinator.execute("task-1", worker)

    assert not result.success
    assert result.error == "build failed"
    assert queue.engine._find("task-1").status == TaskStatus.FAILED


def test_dependencies_are_respected(tmp_path) -> None:
    queue, _, coordinator = make_coordinator(tmp_path)

    queue.add("first", "First task")
    queue.add(
        "second",
        "Second task",
        dependencies=["first"],
    )

    result = coordinator.execute(
        "second",
        lambda task: "should not run",
    )

    assert not result.success
    assert "dependencies" in result.error
    assert queue.engine._find("second").status == TaskStatus.PENDING


def test_run_next_executes_ready_task(tmp_path) -> None:
    queue, _, coordinator = make_coordinator(tmp_path)

    queue.add("task-1", "Build")

    result = coordinator.run_next(
        lambda task: "done",
    )

    assert result is not None
    assert result.success
    assert result.task_id == "task-1"


def test_run_next_returns_none_when_idle(tmp_path) -> None:
    _, _, coordinator = make_coordinator(tmp_path)

    assert coordinator.run_next(lambda task: "done") is None


def test_run_until_idle_executes_dependency_chain(tmp_path) -> None:
    queue, _, coordinator = make_coordinator(tmp_path)

    queue.add("first", "First")
    queue.add(
        "second",
        "Second",
        dependencies=["first"],
    )
    queue.add(
        "third",
        "Third",
        dependencies=["second"],
    )

    results = coordinator.run_until_idle(
        lambda task: task.id,
    )

    assert [result.task_id for result in results] == [
        "first",
        "second",
        "third",
    ]
    assert all(result.success for result in results)


def test_run_until_idle_stops_after_failure(tmp_path) -> None:
    queue, _, coordinator = make_coordinator(tmp_path)

    queue.add("first", "First")
    queue.add("second", "Second")

    def worker(task):
        if task.id == "first":
            raise RuntimeError("failed first")
        return "done"

    results = coordinator.run_until_idle(worker)

    assert len(results) == 1
    assert not results[0].success
    assert queue.engine._find("first").status == TaskStatus.FAILED


def test_run_until_idle_respects_max_tasks(tmp_path) -> None:
    queue, _, coordinator = make_coordinator(tmp_path)

    queue.add("a", "A")
    queue.add("b", "B")
    queue.add("c", "C")

    results = coordinator.run_until_idle(
        lambda task: "done",
        max_tasks=2,
    )

    assert len(results) == 2


def test_max_tasks_must_be_positive(tmp_path) -> None:
    _, _, coordinator = make_coordinator(tmp_path)

    try:
        coordinator.run_until_idle(
            lambda task: "done",
            max_tasks=0,
        )
    except ValueError as exc:
        assert "max_tasks" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_non_pending_task_is_not_executed(tmp_path) -> None:
    queue, _, coordinator = make_coordinator(tmp_path)

    queue.add("task-1", "Build")
    queue.complete("task-1")

    result = coordinator.execute(
        "task-1",
        lambda task: "should not run",
    )

    assert not result.success
    assert "not pending" in result.error
