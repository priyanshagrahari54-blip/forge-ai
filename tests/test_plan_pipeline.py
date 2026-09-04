from forge.agents.execution import CallableAgentExecutor
from forge.agents.planner import AgentPlan, CapabilityAgentPlanner
from forge.agents.registry import AgentRegistration, AgentRegistry
from forge.core.agent_pipeline import AgentPipeline
from forge.core.task_engine import Task, TaskStatus


def make_agent(name, role, capabilities, output="ok"):
    return AgentRegistration(
        name=name,
        role=role,
        executor=CallableAgentExecutor(
            name,
            lambda request, output=output: output,
        ),
        capabilities=capabilities,
    )


def make_registry():
    registry = AgentRegistry()

    registry.register(
        make_agent(
            "debugger",
            "debugging",
            ("debugging",),
            "debugged",
        )
    )

    registry.register(
        make_agent(
            "coder",
            "coding",
            ("coding",),
            "coded",
        )
    )

    registry.register(
        make_agent(
            "tester",
            "testing",
            ("testing",),
            "tested",
        )
    )

    registry.register(
        make_agent(
            "reviewer",
            "reviewing",
            ("review",),
            "reviewed",
        )
    )

    return registry


def make_plan():
    registry = make_registry()
    planner = CapabilityAgentPlanner(registry)

    return planner.plan(
        "Fix the bug, implement the code, run tests, and review the code."
    )


def test_pipeline_executes_agent_plan():
    plan = make_plan()

    pipeline = AgentPipeline(
        agent_registry=make_registry(),
    )

    task = Task(
        id="plan-1",
        description="Fix the bug, implement the code, run tests, and review the code.",
    )

    results = pipeline.execute_plan(task, plan)

    assert len(results) == 4
    assert all(result.success for result in results)
    assert task.status == TaskStatus.COMPLETED


def test_plan_execution_preserves_agent_order():
    plan = make_plan()

    pipeline = AgentPipeline(
        agent_registry=make_registry(),
    )

    task = Task(
        id="plan-2",
        description="Fix the bug, implement the code, run tests, and review the code.",
    )

    results = pipeline.execute_plan(task, plan)

    assert [result.agent for result in results] == [
        "debugger",
        "coder",
        "tester",
        "reviewer",
    ]


def test_plan_execution_maps_capabilities_to_lifecycle_stages():
    plan = make_plan()

    pipeline = AgentPipeline(
        agent_registry=make_registry(),
    )

    task = Task(
        id="plan-3",
        description="Fix the bug, implement the code, run tests, and review the code.",
    )

    results = pipeline.execute_plan(task, plan)

    assert [result.stage for result in results] == [
        TaskStatus.DEBUGGING,
        TaskStatus.CODING,
        TaskStatus.TESTING,
        TaskStatus.REVIEWING,
    ]


def test_plan_execution_preserves_outputs():
    plan = make_plan()

    pipeline = AgentPipeline(
        agent_registry=make_registry(),
    )

    task = Task(
        id="plan-4",
        description="Fix the bug, implement the code, run tests, and review the code.",
    )

    results = pipeline.execute_plan(task, plan)

    assert [result.output for result in results] == [
        "debugged",
        "coded",
        "tested",
        "reviewed",
    ]


def test_plan_execution_stops_on_failure():
    registry = AgentRegistry()

    registry.register(
        AgentRegistration(
            name="debugger",
            role="debugging",
            executor=CallableAgentExecutor(
                "debugger",
                lambda request: (_ for _ in ()).throw(
                    RuntimeError("debug failure")
                ),
            ),
            capabilities=("debugging",),
        )
    )

    registry.register(
        make_agent(
            "coder",
            "coding",
            ("coding",),
            "coded",
        )
    )

    planner = CapabilityAgentPlanner(registry)

    plan = planner.plan(
        "Fix the bug and implement the code."
    )

    pipeline = AgentPipeline(
        agent_registry=registry,
    )

    task = Task(
        id="plan-5",
        description="Fix the bug and implement the code.",
    )

    results = pipeline.execute_plan(task, plan)

    assert len(results) == 1
    assert not results[0].success
    assert "debug failure" in results[0].error
    assert task.status == TaskStatus.FAILED


def test_plan_execution_reuses_context():
    plan = make_plan()

    calls = []

    class Context:
        fingerprint = "plan-context-123"

    def context_provider(task):
        calls.append(task.id)
        return Context()

    pipeline = AgentPipeline(
        agent_registry=make_registry(),
        context_provider=context_provider,
    )

    task = Task(
        id="plan-6",
        description="Fix the bug, implement the code, run tests, and review the code.",
    )

    results = pipeline.execute_plan(task, plan)

    assert calls == ["plan-6"]
    assert all(
        result.context_fingerprint == "plan-context-123"
        for result in results
    )


def test_empty_plan_completes_without_agent_execution():
    pipeline = AgentPipeline(
        agent_registry=make_registry(),
    )

    task = Task(
        id="plan-7",
        description="No matching capability.",
    )

    empty_plan = AgentPlan(
        requirements=CapabilityAgentPlanner(
            make_registry()
        ).extractor.extract(""),
        agents=(),
    )

    results = pipeline.execute_plan(task, empty_plan)

    assert results == []
    assert task.status == TaskStatus.COMPLETED
