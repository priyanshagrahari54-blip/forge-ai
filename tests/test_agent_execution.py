from forge.agents.execution import (
    AgentRequest,
    AgentResponse,
    CallableAgentExecutor,
)
from forge.core.task_engine import Task, TaskStatus


def make_task():
    return Task(
        id="task-1",
        description="Implement the feature",
    )


def make_request():
    task = make_task()
    return AgentRequest(
        task=task,
        stage=TaskStatus.CODING,
        instructions="Implement the requested feature.",
    )


def test_request_contains_task_and_stage():
    request = make_request()

    assert request.task.id == "task-1"
    assert request.stage == TaskStatus.CODING
    assert request.instructions


def test_callable_executor_returns_response():
    executor = CallableAgentExecutor(
        "coder",
        lambda request: f"worked on {request.task.id}",
    )

    response = executor.execute(make_request())

    assert isinstance(response, AgentResponse)
    assert response.success
    assert response.output == "worked on task-1"
    assert response.agent == "coder"
    assert response.stage == TaskStatus.CODING


def test_callable_executor_handles_failure():
    def worker(request):
        raise RuntimeError("model failed")

    executor = CallableAgentExecutor("coder", worker)

    response = executor.execute(make_request())

    assert not response.success
    assert response.error == "model failed"
    assert response.agent == "coder"
    assert response.stage == TaskStatus.CODING


def test_request_metadata_is_preserved():
    request = AgentRequest(
        task=make_task(),
        stage=TaskStatus.PLANNING,
        metadata={"source": "test"},
    )

    assert request.metadata["source"] == "test"


def test_response_metadata_is_preserved():
    response = AgentResponse(
        success=True,
        agent="planner",
        stage=TaskStatus.PLANNING,
        metadata={"model": "test-model"},
    )

    assert response.metadata["model"] == "test-model"
