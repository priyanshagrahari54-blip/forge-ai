from __future__ import annotations

from forge.core.agent_executor import (
    AgentExecutionResult,
    CallableAgentExecutor,
)
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


def test_coordinator_executes_agent(tmp_path) -> None:
    queue, _, coordinator = make_coordinator(tmp_path)
    queue.add("task-1", "Build feature")

    agent = CallableAgentExecutor(
        lambda task: f"agent completed {task.id}",
        agent_name="coder",
    )

    result = coordinator.execute("task-1", agent)

    assert result.success
    assert result.output == "agent completed task-1"
    assert result.agent == "coder"
    assert queue.engine._find("task-1").status == TaskStatus.COMPLETED


def test_agent_failure_marks_task_failed(tmp_path) -> None:
    queue, _, coordinator = make_coordinator(tmp_path)
    queue.add("task-1", "Build feature")

    agent = CallableAgentExecutor(
        lambda task: (_ for _ in ()).throw(RuntimeError("agent failed")),
        agent_name="coder",
    )

    result = coordinator.execute("task-1", agent)

    assert not result.success
    assert result.error == "agent failed"
    assert result.agent == "coder"
    assert queue.engine._find("task-1").status == TaskStatus.FAILED


def test_dependencies_are_checked_before_agent_execution(tmp_path) -> None:
    queue, _, coordinator = make_coordinator(tmp_path)

    queue.add("first", "First task")
    queue.add("second", "Second task", dependencies=["first"])

    executed = []

    agent = CallableAgentExecutor(
        lambda task: executed.append(task.id) or "done",
        agent_name="coder",
    )

    result = coordinator.execute("second", agent)

    assert not result.success
    assert "dependencies" in result.error
    assert executed == []


def test_run_next_uses_agent(tmp_path) -> None:
    queue, _, coordinator = make_coordinator(tmp_path)
    queue.add("task-1", "Build")

    agent = CallableAgentExecutor(
        lambda task: "done",
        agent_name="tester",
    )

    result = coordinator.run_next(agent)

    assert result is not None
    assert result.success
    assert result.agent == "tester"


def test_run_next_returns_none_when_idle(tmp_path) -> None:
    _, _, coordinator = make_coordinator(tmp_path)

    agent = CallableAgentExecutor(lambda task: "done")

    assert coordinator.run_next(agent) is None


def test_run_until_idle_executes_dependency_chain(tmp_path) -> None:
    queue, _, coordinator = make_coordinator(tmp_path)

    queue.add("first", "First")
    queue.add("second", "Second", dependencies=["first"])
    queue.add("third", "Third", dependencies=["second"])

    agent = CallableAgentExecutor(
        lambda task: task.id,
        agent_name="coder",
    )

    results = coordinator.run_until_idle(agent)

    assert [result.task_id for result in results] == [
        "first",
        "second",
        "third",
    ]
    assert all(result.success for result in results)


def test_run_until_idle_stops_on_agent_failure(tmp_path) -> None:
    queue, _, coordinator = make_coordinator(tmp_path)

    queue.add("first", "First")
    queue.add("second", "Second")

    def worker(task):
        if task.id == "first":
            raise RuntimeError("first failed")
        return "done"

    agent = CallableAgentExecutor(worker)

    results = coordinator.run_until_idle(agent)

    assert len(results) == 1
    assert not results[0].success
    assert queue.engine._find("first").status == TaskStatus.FAILED


def test_run_until_idle_respects_max_tasks(tmp_path) -> None:
    queue, _, coordinator = make_coordinator(tmp_path)

    queue.add("a", "A")
    queue.add("b", "B")
    queue.add("c", "C")

    agent = CallableAgentExecutor(lambda task: "done")

    results = coordinator.run_until_idle(agent, max_tasks=2)

    assert len(results) == 2


def test_max_tasks_must_be_positive(tmp_path) -> None:
    _, _, coordinator = make_coordinator(tmp_path)

    agent = CallableAgentExecutor(lambda task: "done")

    try:
        coordinator.run_until_idle(agent, max_tasks=0)
    except ValueError as exc:
        assert "max_tasks" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_non_pending_task_is_not_executed(tmp_path) -> None:
    queue, _, coordinator = make_coordinator(tmp_path)

    queue.add("task-1", "Build")
    queue.complete("task-1")

    executed = []

    agent = CallableAgentExecutor(
        lambda task: executed.append(task.id) or "done",
    )

    result = coordinator.execute("task-1", agent)

    assert not result.success
    assert "not pending" in result.error
    assert executed == []


def test_agent_result_is_persisted_through_task_completion(tmp_path) -> None:
    queue, _, coordinator = make_coordinator(tmp_path)

    queue.add("task-1", "Build")

    agent = CallableAgentExecutor(
        lambda task: "build successful",
        agent_name="coder",
    )

    result = coordinator.execute("task-1", agent)

    stored = queue.store.load("task-1")

    assert result.output == "build successful"
    assert stored.status == TaskStatus.COMPLETED
    assert stored.attempts == 1
