import pytest

from forge.agents.execution import CallableAgentExecutor
from forge.agents.registry import AgentRegistration, AgentRegistry


def make_registration(name="coder", role="coding"):
    executor = CallableAgentExecutor(
        name,
        lambda request: "ok",
    )

    return AgentRegistration(
        name=name,
        role=role,
        executor=executor,
    )


def test_register_and_get_agent():
    registry = AgentRegistry()

    registration = make_registration()
    registry.register(registration)

    assert registry.get("coder") is registration
    assert registry.has("coder")
    assert len(registry) == 1


def test_get_by_role_is_deterministic():
    registry = AgentRegistry(
        [
            make_registration("zeta", "coding"),
            make_registration("alpha", "coding"),
            make_registration("tester", "testing"),
        ]
    )

    agents = registry.get_by_role("coding")

    assert [agent.name for agent in agents] == [
        "alpha",
        "zeta",
    ]


def test_names_and_roles_are_sorted():
    registry = AgentRegistry(
        [
            make_registration("reviewer", "review"),
            make_registration("coder", "coding"),
            make_registration("planner", "planning"),
        ]
    )

    assert registry.names() == [
        "coder",
        "planner",
        "reviewer",
    ]

    assert registry.roles() == [
        "coding",
        "planning",
        "review",
    ]


def test_duplicate_registration_is_rejected():
    registry = AgentRegistry()
    registry.register(make_registration())

    with pytest.raises(ValueError, match="already registered"):
        registry.register(make_registration())


def test_replace_updates_registration():
    registry = AgentRegistry()
    registry.register(make_registration("coder", "coding"))

    replacement = make_registration("coder", "advanced-coding")
    registry.replace(replacement)

    assert registry.get("coder") is replacement
    assert registry.get("coder").role == "advanced-coding"


def test_unknown_agent_is_rejected():
    registry = AgentRegistry()

    with pytest.raises(KeyError, match="Unknown agent"):
        registry.get("missing")


def test_remove_agent():
    registry = AgentRegistry()
    registry.register(make_registration())

    registry.remove("coder")

    assert not registry.has("coder")
    assert len(registry) == 0


def test_invalid_registration_is_rejected():
    registry = AgentRegistry()

    with pytest.raises(ValueError, match="name"):
        registry.register(make_registration(name=""))

    with pytest.raises(ValueError, match="role"):
        registry.register(make_registration(role=""))
