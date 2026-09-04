from __future__ import annotations

from forge.core.agent_pipeline import AgentPipeline
from forge.core.task_coordinator import TaskExecutionCoordinator
from forge.core.task_engine import TaskStatus
from forge.core.task_queue import PersistentTaskQueue
from forge.core.task_recovery import TaskRecoveryEngine
from forge.core.task_store import TaskStore


def make_coordinator(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    queue = PersistentTaskQueue(store=store)
    recovery = TaskRecoveryEngine(store)
    return queue, TaskExecutionCoordinator(queue, recovery)


def test_coordinator_executes_pipeline(tmp_path) -> None:
    queue, coordinator = make_coordinator(tmp_path)
    queue.add("task-1", "Build feature")

    stages = []

    def handler(task, stage):
        stages.append(stage)
        return f"{stage.value} complete"

    pipeline = AgentPipeline(handler)

    result = coordinator.execute_pipeline("task-1", pipeline)

    assert result.success
    assert result.task_id == "task-1"
    assert len(result.stages) == 4
    assert queue.engine._find("task-1").status == TaskStatus.COMPLETED


def test_pipeline_stage_results_are_returned(tmp_path) -> None:
    queue, coordinator = make_coordinator(tmp_path)
    queue.add("task-1", "Build")

    pipeline = AgentPipeline(
        lambda task, stage: stage.value,
    )

    result = coordinator.execute_pipeline("task-1", pipeline)

    assert [stage.output for stage in result.stages] == [
        "planning",
        "coding",
        "testing",
        "reviewing",
    ]


def test_pipeline_failure_marks_task_failed(tmp_path) -> None:
    queue, coordinator = make_coordinator(tmp_path)
    queue.add("task-1", "Build")

    def handler(task, stage):
        if stage == TaskStatus.TESTING:
            raise RuntimeError("tests failed")
        return "ok"

    result = coordinator.execute_pipeline(
        "task-1",
        AgentPipeline(handler),
    )

    assert not result.success
    assert result.error == "tests failed"
    assert queue.engine._find("task-1").status == TaskStatus.FAILED


def test_pipeline_respects_dependencies(tmp_path) -> None:
    queue, coordinator = make_coordinator(tmp_path)

    queue.add("first", "First")
    queue.add("second", "Second", dependencies=["first"])

    executed = []

    pipeline = AgentPipeline(
        lambda task, stage: executed.append(stage) or "ok",
    )

    result = coordinator.execute_pipeline("second", pipeline)

    assert not result.success
    assert "dependencies" in result.error
    assert executed == []


def test_run_pipeline_next(tmp_path) -> None:
    queue, coordinator = make_coordinator(tmp_path)
    queue.add("task-1", "Build")

    pipeline = AgentPipeline(
        lambda task, stage: "done",
    )

    result = coordinator.run_pipeline_next(pipeline)

    assert result is not None
    assert result.success


def test_run_pipeline_until_idle_dependency_chain(tmp_path) -> None:
    queue, coordinator = make_coordinator(tmp_path)

    queue.add("first", "First")
    queue.add("second", "Second", dependencies=["first"])
    queue.add("third", "Third", dependencies=["second"])

    pipeline = AgentPipeline(
        lambda task, stage: f"{task.id}:{stage.value}",
    )

    results = coordinator.run_pipeline_until_idle(pipeline)

    assert [result.task_id for result in results] == [
        "first",
        "second",
        "third",
    ]
    assert all(result.success for result in results)


def test_pipeline_persists_completion(tmp_path) -> None:
    queue, coordinator = make_coordinator(tmp_path)
    queue.add("task-1", "Build")

    pipeline = AgentPipeline(
        lambda task, stage: "success",
    )

    coordinator.execute_pipeline("task-1", pipeline)

    stored = queue.store.load("task-1")

    assert stored.status == TaskStatus.COMPLETED
    assert stored.attempts == 1


def test_pipeline_stops_after_failure(tmp_path) -> None:
    queue, coordinator = make_coordinator(tmp_path)

    queue.add("first", "First")
    queue.add("second", "Second", dependencies=["first"])

    def handler(task, stage):
        if task.id == "first" and stage == TaskStatus.CODING:
            raise RuntimeError("coding failed")
        return "ok"

    pipeline = AgentPipeline(handler)

    results = coordinator.run_pipeline_until_idle(pipeline)

    assert len(results) == 1
    assert not results[0].success
    assert queue.engine._find("first").status == TaskStatus.FAILED
    assert queue.engine._find("second").status == TaskStatus.PENDING


def test_pipeline_max_tasks(tmp_path) -> None:
    queue, coordinator = make_coordinator(tmp_path)

    queue.add("a", "A")
    queue.add("b", "B")
    queue.add("c", "C")

    pipeline = AgentPipeline(
        lambda task, stage: "ok",
    )

    results = coordinator.run_pipeline_until_idle(
        pipeline,
        max_tasks=2,
    )

    assert len(results) == 2


def test_pipeline_max_tasks_validation(tmp_path) -> None:
    _, coordinator = make_coordinator(tmp_path)

    pipeline = AgentPipeline(
        lambda task, stage: "ok",
    )

    try:
        coordinator.run_pipeline_until_idle(
            pipeline,
            max_tasks=0,
        )
    except ValueError as exc:
        assert "max_tasks" in str(exc)
    else:
        raise AssertionError("Expected ValueError")
