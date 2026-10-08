from forge.agents.execution import AgentExecutor, AgentResponse
from forge.agents.runner import AgentRunner
from forge.agents.factory import AgentDefinition
from forge.core.task_engine import TaskStatus


class _Executor(AgentExecutor):
    name = "dynamic-security"

    def execute(self, request):
        return AgentResponse(
            success=True,
            output="executed",
            agent=self.name,
            stage=request.stage,
        )


def test_dynamic_role_can_run_when_trusted_executor_exists():
    definition = AgentDefinition(
        name="security-specialist",
        role="security",
        capabilities=("security",),
        executor="security",
        real=True,
    )
    runner = AgentRunner("session", lambda role: _Executor() if role == "security" else None)
    result = runner.run(definition, "audit the authorized test target")
    assert result.success is True
    assert result.output == "executed"
    assert result.role == "security"


def test_unknown_dynamic_role_stays_unexecutable_without_executor():
    definition = AgentDefinition(
        name="custom-specialist",
        role="custom-specialist",
        capabilities=("research",),
        executor="custom-specialist",
        real=True,
    )
    runner = AgentRunner("session", lambda role: None)
    try:
        runner.run(definition, "do work")
    except ValueError as exc:
        assert "No executor could be built" in str(exc)
    else:
        raise AssertionError("unregistered executor must not execute")
