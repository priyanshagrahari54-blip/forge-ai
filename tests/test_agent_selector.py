from forge.agents.execution import CallableAgentExecutor
from forge.agents.registry import AgentRegistration, AgentRegistry
from forge.agents.selector import AgentSelector


def make_agent(name, role, capabilities):
    return AgentRegistration(
        name=name,
        role=role,
        executor=CallableAgentExecutor(name, lambda request: "ok"),
        capabilities=capabilities,
    )


def test_selector_finds_agents_by_capability():
    registry = AgentRegistry()

    registry.register(make_agent("coder", "coding", ("python", "refactoring")))
    registry.register(make_agent("debugger", "debugging", ("python", "debugging")))

    selector = AgentSelector(registry)

    candidates = selector.candidates("python")

    assert [candidate.registration.name for candidate in candidates] == [
        "coder",
        "debugger",
    ]


def test_selector_filters_by_role():
    registry = AgentRegistry()

    registry.register(make_agent("coder", "coding", ("python",)))
    registry.register(make_agent("reviewer", "reviewing", ("python",)))

    selector = AgentSelector(registry)

    candidates = selector.candidates("python", role="reviewing")

    assert [candidate.registration.name for candidate in candidates] == [
        "reviewer"
    ]


def test_selector_returns_best_candidate():
    registry = AgentRegistry()

    registry.register(make_agent("coder", "coding", ("python",)))
    registry.register(make_agent("reviewer", "reviewing", ("python",)))

    selector = AgentSelector(registry)

    selected = selector.select("python", role="reviewing")

    assert selected is not None
    assert selected.name == "reviewer"


def test_selector_is_deterministic_for_equal_candidates():
    registry = AgentRegistry()

    registry.register(make_agent("zeta", "coding", ("python",)))
    registry.register(make_agent("alpha", "coding", ("python",)))

    selector = AgentSelector(registry)

    candidates = selector.candidates("python")

    assert [candidate.registration.name for candidate in candidates] == [
        "alpha",
        "zeta",
    ]


def test_selector_returns_none_when_no_match():
    registry = AgentRegistry()
    registry.register(make_agent("coder", "coding", ("python",)))

    selector = AgentSelector(registry)

    assert selector.select("security") is None
