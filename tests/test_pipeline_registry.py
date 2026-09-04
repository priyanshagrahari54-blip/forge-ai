from forge.agents.registry import AgentRegistry, AgentRegistration
from forge.agents.execution import CallableAgentExecutor
from forge.core.agent_pipeline import AgentPipeline
from forge.core.task_engine import Task, TaskStatus


def make_task():
    return Task(id="t1", description="test task")


def make_registry():
    registry = AgentRegistry()

    for role, stage in [
        ("planning", TaskStatus.PLANNING),
        ("coding", TaskStatus.CODING),
        ("testing", TaskStatus.TESTING),
        ("reviewing", TaskStatus.REVIEWING),
    ]:
        executor = CallableAgentExecutor(
            role,
            lambda request, role=role: f"{role} completed",
        )
        registry.register(
            AgentRegistration(
                name=role,
                role=role,
                executor=executor,
            )
        )

    return registry


def test_pipeline_resolves_agents_from_registry():
    pipeline = AgentPipeline(agent_registry=make_registry())

    results = pipeline.execute(make_task())

    assert len(results) == 4
    assert all(result.success for result in results)


def test_registry_agent_outputs_are_preserved():
    pipeline = AgentPipeline(agent_registry=make_registry())

    results = pipeline.execute(make_task())

    outputs = [result.output for result in results]

    assert outputs == [
        "planning completed",
        "coding completed",
        "testing completed",
        "reviewing completed",
    ]


def test_missing_registry_agent_fails_stage():
    registry = AgentRegistry()

    registry.register(
        AgentRegistration(
            name="planner",
            role="planning",
            executor=CallableAgentExecutor(
                "planner",
                lambda request: "planned",
            ),
        )
    )

    pipeline = AgentPipeline(agent_registry=registry)

    results = pipeline.execute(make_task())

    assert results[0].success
    assert not results[1].success
    assert "No agent registered" in results[1].error


def test_registry_cannot_be_combined_with_stage_agents():
    import pytest

    registry = make_registry()

    with pytest.raises(ValueError, match="stage_agents or agent_registry"):
        AgentPipeline(
            agent_registry=registry,
            stage_agents={},
        )


def test_legacy_stage_handler_still_works():
    pipeline = AgentPipeline(
        stage_handler=lambda task, stage: f"{stage.value} handled"
    )

    results = pipeline.execute(make_task())

    assert len(results) == 4
    assert all(result.success for result in results)
