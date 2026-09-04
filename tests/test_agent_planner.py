from forge.agents.execution import CallableAgentExecutor
from forge.agents.planner import CapabilityAgentPlanner
from forge.agents.registry import AgentRegistration, AgentRegistry


def make_agent(name, role, capabilities):
    return AgentRegistration(
        name=name,
        role=role,
        executor=CallableAgentExecutor(
            name,
            lambda request: "ok",
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
        )
    )
    registry.register(
        make_agent(
            "coder",
            "coding",
            ("coding",),
        )
    )
    registry.register(
        make_agent(
            "tester",
            "testing",
            ("testing",),
        )
    )
    registry.register(
        make_agent(
            "reviewer",
            "reviewing",
            ("review",),
        )
    )

    return registry


def test_planner_builds_multi_agent_plan():
    planner = CapabilityAgentPlanner(make_registry())

    plan = planner.plan(
        "Fix the bug, implement the code, run tests, and review the code."
    )

    assert plan.capabilities == (
        "debugging",
        "coding",
        "testing",
        "review",
    )

    assert plan.names == (
        "debugger",
        "coder",
        "tester",
        "reviewer",
    )


def test_planner_preserves_requirement_information():
    planner = CapabilityAgentPlanner(make_registry())

    plan = planner.plan(
        "Fix the bug and run regression tests."
    )

    assert plan.requirements.capabilities == (
        "debugging",
        "testing",
    )


def test_planner_is_deterministic():
    planner = CapabilityAgentPlanner(make_registry())

    first = planner.plan(
        "Fix the bug, implement the feature, and test it."
    )
    second = planner.plan(
        "Fix the bug, implement the feature, and test it."
    )

    assert first == second


def test_planner_skips_unavailable_capabilities():
    registry = AgentRegistry()

    registry.register(
        make_agent(
            "coder",
            "coding",
            ("coding",),
        )
    )

    planner = CapabilityAgentPlanner(registry)

    plan = planner.plan(
        "Implement the feature and perform a security review."
    )

    assert plan.names == ("coder",)
    assert plan.capabilities == ("coding",)


def test_planner_does_not_duplicate_same_agent():
    registry = AgentRegistry()

    registry.register(
        make_agent(
            "generalist",
            "coding",
            ("coding", "debugging"),
        )
    )

    planner = CapabilityAgentPlanner(registry)

    plan = planner.plan(
        "Fix the bug and implement the feature."
    )

    assert plan.names == ("generalist",)
    assert len(plan.agents) == 1


def test_empty_task_produces_empty_plan():
    planner = CapabilityAgentPlanner(make_registry())

    plan = planner.plan("")

    assert plan.is_empty()
    assert plan.names == ()
    assert plan.capabilities == ()
