from __future__ import annotations

from forge.core.pipeline_agents import (
    CoderStageAgent,
    PlannerAgent,
    ReviewerStageAgent,
    StageAgentResult,
    TesterStageAgent,
)
from forge.core.task_engine import Task, TaskStatus


def make_task() -> Task:
    return Task(
        id="task-1",
        description="Build feature",
    )


def test_planner_agent() -> None:
    agent = PlannerAgent(lambda task: "plan created")

    result = agent.execute(make_task())

    assert isinstance(result, StageAgentResult)
    assert result.agent == "planner"
    assert result.stage == TaskStatus.PLANNING
    assert result.success
    assert result.output == "plan created"


def test_coder_agent() -> None:
    agent = CoderStageAgent(lambda task: "code written")

    result = agent.execute(make_task())

    assert result.agent == "coder"
    assert result.stage == TaskStatus.CODING
    assert result.success
    assert result.output == "code written"


def test_tester_agent() -> None:
    agent = TesterStageAgent(lambda task: "tests passed")

    result = agent.execute(make_task())

    assert result.agent == "tester"
    assert result.stage == TaskStatus.TESTING
    assert result.success
    assert result.output == "tests passed"


def test_reviewer_agent() -> None:
    agent = ReviewerStageAgent(lambda task: "review passed")

    result = agent.execute(make_task())

    assert result.agent == "reviewer"
    assert result.stage == TaskStatus.REVIEWING
    assert result.success
    assert result.output == "review passed"


def test_stage_agent_failure() -> None:
    agent = CoderStageAgent(
        lambda task: (_ for _ in ()).throw(
            RuntimeError("compiler error")
        )
    )

    result = agent.execute(make_task())

    assert not result.success
    assert result.agent == "coder"
    assert result.stage == TaskStatus.CODING
    assert result.error == "compiler error"


def test_stage_agent_converts_output_to_string() -> None:
    agent = TesterStageAgent(lambda task: 123)

    result = agent.execute(make_task())

    assert result.success
    assert result.output == "123"
