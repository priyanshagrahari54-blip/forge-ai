from __future__ import annotations

import pytest

class FakeExecutor:
    def __init__(self, name: str) -> None:
        self.name = name

    def execute(self, request):
        return None

from forge.agents.planner import AgentPlan, PlannedAgent
from forge.agents.registry import AgentRegistration, AgentRegistry
from forge.agents.validator import (
    AgentPlanValidator,
    PlanValidationCode,
)


def executor(output: str = "ok") -> FakeExecutor:
    return FakeExecutor(output)


def registration(
    name: str,
    role: str,
    capabilities: tuple[str, ...],
) -> AgentRegistration:
    return AgentRegistration(
        name=name,
        role=role,
        executor=executor(name),
        capabilities=capabilities,
    )


def plan_for(*items: tuple[str, AgentRegistration]) -> AgentPlan:
    agents = tuple(
        PlannedAgent(
            capability=capability,
            registration=agent,
            order=index,
        )
        for index, (capability, agent) in enumerate(items)
    )
    from forge.agents.requirements import TaskRequirements

    return AgentPlan(
        requirements=TaskRequirements(
            capabilities=tuple(capability for capability, _ in items)
        ),
        agents=agents,
    )


def test_valid_plan_passes() -> None:
    registry = AgentRegistry()
    coder = registration("coder", "coding", ("coding",))
    registry.register(coder)

    result = AgentPlanValidator(registry).validate(
        plan_for(("coding", coder))
    )

    assert result.valid
    assert result.issues == ()


def test_empty_plan_is_rejected() -> None:
    from forge.agents.requirements import TaskRequirements

    registry = AgentRegistry()
    plan = AgentPlan(requirements=TaskRequirements(), agents=())

    result = AgentPlanValidator(registry).validate(plan)

    assert not result.valid
    assert result.issues[0].code == PlanValidationCode.EMPTY_PLAN


def test_unknown_agent_is_rejected() -> None:
    registry = AgentRegistry()
    coder = registration("coder", "coding", ("coding",))

    result = AgentPlanValidator(registry).validate(
        plan_for(("coding", coder))
    )

    assert not result.valid
    assert any(
        issue.code == PlanValidationCode.UNKNOWN_AGENT
        for issue in result.issues
    )


def test_capability_mismatch_is_rejected() -> None:
    registry = AgentRegistry()
    coder = registration("coder", "coding", ("coding",))
    registry.register(coder)

    result = AgentPlanValidator(registry).validate(
        plan_for(("testing", coder))
    )

    assert not result.valid
    assert any(
        issue.code == PlanValidationCode.CAPABILITY_MISMATCH
        for issue in result.issues
    )


def test_role_mismatch_is_rejected() -> None:
    registry = AgentRegistry()
    tester = registration("tester", "testing", ("testing",))
    registry.register(tester)

    result = AgentPlanValidator(registry).validate(
        plan_for(("coding", tester))
    )

    assert not result.valid
    assert any(
        issue.code == PlanValidationCode.ROLE_MISMATCH
        for issue in result.issues
    )


def test_invalid_stage_mapping_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = AgentRegistry()
    specialist = registration("specialist", "specialist", ("specialist",))
    registry.register(specialist)

    monkeypatch.setattr(
        "forge.agents.validator.CAPABILITY_STAGES",
        {},
    )

    result = AgentPlanValidator(registry).validate(
        plan_for(("specialist", specialist))
    )

    assert not result.valid
    assert any(
        issue.code == PlanValidationCode.INVALID_STAGE
        for issue in result.issues
    )


def test_duplicate_agent_is_rejected() -> None:
    registry = AgentRegistry()
    coder = registration("coder", "coding", ("coding", "testing"))
    registry.register(coder)

    result = AgentPlanValidator(registry).validate(
        plan_for(
            ("coding", coder),
            ("testing", coder),
        )
    )

    assert not result.valid
    assert any(
        issue.code == PlanValidationCode.DUPLICATE_AGENT
        for issue in result.issues
    )


def test_validate_or_raise_does_not_raise_for_valid_plan() -> None:
    registry = AgentRegistry()
    coder = registration("coder", "coding", ("coding",))
    registry.register(coder)

    AgentPlanValidator(registry).validate_or_raise(
        plan_for(("coding", coder))
    )


def test_validate_or_raise_raises_for_invalid_plan() -> None:
    registry = AgentRegistry()

    from forge.agents.requirements import TaskRequirements

    plan = AgentPlan(
        requirements=TaskRequirements(capabilities=("coding",)),
        agents=(),
    )

    with pytest.raises(ValueError, match="Invalid agent plan"):
        AgentPlanValidator(registry).validate_or_raise(plan)


def test_validation_preserves_deterministic_issue_order() -> None:
    registry = AgentRegistry()
    coder = registration("coder", "testing", ("testing",))
    registry.register(coder)

    result = AgentPlanValidator(registry).validate(
        plan_for(
            ("coding", coder),
            ("testing", coder),
        )
    )

    assert [issue.code for issue in result.issues] == [
        PlanValidationCode.CAPABILITY_MISMATCH,
        PlanValidationCode.ROLE_MISMATCH,
        PlanValidationCode.DUPLICATE_AGENT,
    ]


def test_known_capabilities_have_stage_mappings() -> None:
    registry = AgentRegistry()
    validator = AgentPlanValidator(registry)

    expected = {
        "planning",
        "coding",
        "testing",
        "debugging",
        "review",
        "security",
        "documentation",
    }

    assert set(
        validator._role_for_capability(capability)
        for capability in expected
    ) == {
        "planning",
        "coding",
        "testing",
        "debugging",
        "reviewing",
        "security",
        "documentation",
    }
