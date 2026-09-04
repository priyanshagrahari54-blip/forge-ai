from forge.agents.execution import AgentRequest, AgentResponse, CallableAgentExecutor
from forge.agents.stage_executor import ExecutorStageAgent
from forge.core.task_engine import Task, TaskStatus


def make_task():
    return Task(
        id="task-1",
        description="Implement feature",
    )


def test_executor_stage_agent_success():
    executor = CallableAgentExecutor(
        "coder",
        lambda request: "implemented",
    )

    agent = ExecutorStageAgent(
        executor,
        TaskStatus.CODING,
    )

    result = agent.execute(make_task())

    assert result.success
    assert result.agent == "coder"
    assert result.stage == TaskStatus.CODING
    assert result.output == "implemented"


def test_executor_receives_structured_request():
    captured = {}

    def worker(request):
        captured["request"] = request
        return "ok"

    executor = CallableAgentExecutor("planner", worker)

    agent = ExecutorStageAgent(
        executor,
        TaskStatus.PLANNING,
    )

    agent.execute(make_task())

    request = captured["request"]

    assert isinstance(request, AgentRequest)
    assert request.task.id == "task-1"
    assert request.stage == TaskStatus.PLANNING


def test_executor_failure_becomes_stage_failure():
    def worker(request):
        raise RuntimeError("execution failed")

    executor = CallableAgentExecutor("tester", worker)

    agent = ExecutorStageAgent(
        executor,
        TaskStatus.TESTING,
    )

    result = agent.execute(make_task())

    assert not result.success
    assert result.agent == "tester"
    assert result.stage == TaskStatus.TESTING
    assert result.error == "execution failed"


def test_response_fields_are_preserved():
    class CustomExecutor:
        name = "reviewer"

        def execute(self, request):
            return AgentResponse(
                success=True,
                output="review complete",
                agent="reviewer",
                stage=TaskStatus.REVIEWING,
                metadata={"model": "test"},
            )

    agent = ExecutorStageAgent(
        CustomExecutor(),
        TaskStatus.REVIEWING,
    )

    result = agent.execute(make_task())

    assert result.success
    assert result.output == "review complete"
    assert result.agent == "reviewer"
    assert result.stage == TaskStatus.REVIEWING
