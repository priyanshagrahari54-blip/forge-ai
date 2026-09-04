from __future__ import annotations

from forge.core.agent_executor import (
    AgentExecutionResult,
    CallableAgentExecutor,
)
from forge.core.task_engine import Task


def make_task() -> Task:
    return Task(
        id="task-1",
        description="Build feature",
    )


def test_successful_agent_execution() -> None:
    executor = CallableAgentExecutor(
        lambda task: f"completed {task.id}",
        agent_name="coder",
    )

    result = executor.execute(make_task())

    assert isinstance(result, AgentExecutionResult)
    assert result.success
    assert result.output == "completed task-1"
    assert result.agent == "coder"
    assert result.error == ""


def test_failed_agent_execution() -> None:
    def worker(task):
        raise RuntimeError("agent failed")

    executor = CallableAgentExecutor(
        worker,
        agent_name="coder",
    )

    result = executor.execute(make_task())

    assert not result.success
    assert result.error == "agent failed"
    assert result.agent == "coder"


def test_agent_result_defaults() -> None:
    result = AgentExecutionResult(success=True)

    assert result.success
    assert result.output == ""
    assert result.error == ""
    assert result.agent == ""


def test_worker_output_is_converted_to_string() -> None:
    executor = CallableAgentExecutor(
        lambda task: 123,
        agent_name="tester",
    )

    result = executor.execute(make_task())

    assert result.success
    assert result.output == "123"
    assert result.agent == "tester"
