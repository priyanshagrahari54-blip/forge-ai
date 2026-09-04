from __future__ import annotations

from forge.core.agent_pipeline import AgentPipeline
from forge.core.pipeline_agents import (
    CoderStageAgent,
    PlannerAgent,
    ReviewerStageAgent,
    TesterStageAgent,
)
from forge.core.task_engine import Task, TaskStatus


def make_task() -> Task:
    return Task(
        id="task-1",
        description="Implement a feature",
    )


def make_agents(calls: list[str]):
    return {
        TaskStatus.PLANNING: PlannerAgent(
            lambda task: calls.append("planner") or "plan"
        ),
        TaskStatus.CODING: CoderStageAgent(
            lambda task: calls.append("coder") or "code"
        ),
        TaskStatus.TESTING: TesterStageAgent(
            lambda task: calls.append("tester") or "tests"
        ),
        TaskStatus.REVIEWING: ReviewerStageAgent(
            lambda task: calls.append("reviewer") or "review"
        ),
    }


def test_pipeline_routes_each_stage_to_correct_agent() -> None:
    calls: list[str] = []

    pipeline = AgentPipeline(stage_agents=make_agents(calls))
    results = pipeline.execute(make_task())

    assert calls == ["planner", "coder", "tester", "reviewer"]
    assert len(results) == 4
    assert all(result.success for result in results)


def test_pipeline_preserves_agent_names() -> None:
    pipeline = AgentPipeline(
        stage_agents={
            TaskStatus.PLANNING: PlannerAgent(lambda task: "plan"),
            TaskStatus.CODING: CoderStageAgent(lambda task: "code"),
            TaskStatus.TESTING: TesterStageAgent(lambda task: "tests"),
            TaskStatus.REVIEWING: ReviewerStageAgent(lambda task: "review"),
        }
    )

    results = pipeline.execute(make_task())

    assert [result.agent for result in results] == [
        "planner",
        "coder",
        "tester",
        "reviewer",
    ]


def test_pipeline_preserves_stage_outputs() -> None:
    pipeline = AgentPipeline(
        stage_agents={
            TaskStatus.PLANNING: PlannerAgent(lambda task: "PLAN"),
            TaskStatus.CODING: CoderStageAgent(lambda task: "CODE"),
            TaskStatus.TESTING: TesterStageAgent(lambda task: "TEST"),
            TaskStatus.REVIEWING: ReviewerStageAgent(lambda task: "REVIEW"),
        }
    )

    results = pipeline.execute(make_task())

    assert [result.output for result in results] == [
        "PLAN",
        "CODE",
        "TEST",
        "REVIEW",
    ]


def test_missing_agent_fails_pipeline() -> None:
    pipeline = AgentPipeline(
        stage_agents={
            TaskStatus.PLANNING: PlannerAgent(lambda task: "plan"),
        }
    )

    task = make_task()
    results = pipeline.execute(task)

    assert len(results) == 2
    assert results[0].success
    assert not results[1].success
    assert "No agent registered" in results[1].error
    assert task.status == TaskStatus.FAILED


def test_agent_failure_stops_pipeline() -> None:
    pipeline = AgentPipeline(
        stage_agents={
            TaskStatus.PLANNING: PlannerAgent(lambda task: "plan"),
            TaskStatus.CODING: CoderStageAgent(
                lambda task: (_ for _ in ()).throw(
                    RuntimeError("coding failed")
                )
            ),
            TaskStatus.TESTING: TesterStageAgent(lambda task: "tests"),
            TaskStatus.REVIEWING: ReviewerStageAgent(lambda task: "review"),
        }
    )

    task = make_task()
    results = pipeline.execute(task)

    assert len(results) == 2
    assert results[0].success
    assert not results[1].success
    assert results[1].error == "coding failed"
    assert task.status == TaskStatus.FAILED


def test_pipeline_requires_an_execution_strategy() -> None:
    try:
        AgentPipeline()
    except ValueError as exc:
        assert "stage_handler" in str(exc) and "stage_agents" in str(exc) and "agent_registry" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_pipeline_rejects_both_execution_strategies() -> None:
    handler = lambda task, stage: "legacy"

    try:
        AgentPipeline(
            stage_handler=handler,
            stage_agents={
                TaskStatus.PLANNING: PlannerAgent(lambda task: "plan"),
            },
        )
    except ValueError as exc:
        assert "not both" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_legacy_handler_remains_supported() -> None:
    calls: list[TaskStatus] = []

    def handler(task: Task, stage: TaskStatus) -> str:
        calls.append(stage)
        return stage.value

    pipeline = AgentPipeline(stage_handler=handler)
    task = make_task()
    results = pipeline.execute(task)

    assert calls == list(AgentPipeline.STAGES)
    assert all(result.success for result in results)
    assert task.status == TaskStatus.COMPLETED
