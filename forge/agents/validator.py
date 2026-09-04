from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from forge.agents.planner import AgentPlan
from forge.agents.registry import AgentRegistry
CAPABILITY_STAGES = {
    "planning": "planning",
    "coding": "coding",
    "testing": "testing",
    "debugging": "debugging",
    "review": "reviewing",
    "security": "reviewing",
    "documentation": "running",
}


class PlanValidationCode(str, Enum):
    VALID = "valid"
    EMPTY_PLAN = "empty_plan"
    MISSING_CAPABILITY = "missing_capability"
    UNKNOWN_AGENT = "unknown_agent"
    CAPABILITY_MISMATCH = "capability_mismatch"
    ROLE_MISMATCH = "role_mismatch"
    INVALID_STAGE = "invalid_stage"
    DUPLICATE_AGENT = "duplicate_agent"


@dataclass(frozen=True)
class PlanValidationIssue:
    code: PlanValidationCode
    message: str
    capability: str = ""
    agent: str = ""


@dataclass(frozen=True)
class PlanValidationResult:
    valid: bool
    issues: tuple[PlanValidationIssue, ...] = ()

    @property
    def errors(self) -> tuple[PlanValidationIssue, ...]:
        return self.issues

    @property
    def messages(self) -> tuple[str, ...]:
        return tuple(issue.message for issue in self.issues)


class AgentPlanValidator:
    """Validate an AgentPlan against the currently registered agents."""

    def __init__(self, registry: AgentRegistry) -> None:
        self.registry = registry

    def validate(self, plan: AgentPlan) -> PlanValidationResult:
        issues: list[PlanValidationIssue] = []

        if plan.is_empty():
            issues.append(
                PlanValidationIssue(
                    code=PlanValidationCode.EMPTY_PLAN,
                    message="Agent plan contains no executable agents.",
                )
            )
            return PlanValidationResult(valid=False, issues=tuple(issues))

        seen_agents: set[str] = set()

        for planned in plan.agents:
            capability = planned.capability
            registration = planned.registration
            agent_name = registration.name

            if agent_name in seen_agents:
                issues.append(
                    PlanValidationIssue(
                        code=PlanValidationCode.DUPLICATE_AGENT,
                        message=f"Agent '{agent_name}' appears more than once in the plan.",
                        capability=capability,
                        agent=agent_name,
                    )
                )
            seen_agents.add(agent_name)

            try:
                registered = self.registry.get(agent_name)
            except KeyError:
                registered = None

            if registered is None:
                issues.append(
                    PlanValidationIssue(
                        code=PlanValidationCode.UNKNOWN_AGENT,
                        message=f"Agent '{agent_name}' is not registered.",
                        capability=capability,
                        agent=agent_name,
                    )
                )
                continue

            if registered.capabilities and capability not in registered.capabilities:
                issues.append(
                    PlanValidationIssue(
                        code=PlanValidationCode.CAPABILITY_MISMATCH,
                        message=(
                            f"Agent '{agent_name}' does not advertise capability "
                            f"'{capability}'."
                        ),
                        capability=capability,
                        agent=agent_name,
                    )
                )

            expected_role = self._role_for_capability(capability)
            if expected_role and registered.role != expected_role:
                issues.append(
                    PlanValidationIssue(
                        code=PlanValidationCode.ROLE_MISMATCH,
                        message=(
                            f"Agent '{agent_name}' has role '{registered.role}', "
                            f"but capability '{capability}' expects role "
                            f"'{expected_role}'."
                        ),
                        capability=capability,
                        agent=agent_name,
                    )
                )

            if capability not in CAPABILITY_STAGES:
                issues.append(
                    PlanValidationIssue(
                        code=PlanValidationCode.INVALID_STAGE,
                        message=(
                            f"Capability '{capability}' has no lifecycle stage "
                            "mapping."
                        ),
                        capability=capability,
                        agent=agent_name,
                    )
                )

        return PlanValidationResult(
            valid=not issues,
            issues=tuple(issues),
        )

    def validate_or_raise(self, plan: AgentPlan) -> None:
        result = self.validate(plan)
        if result.valid:
            return

        details = "; ".join(result.messages)
        raise ValueError(f"Invalid agent plan: {details}")

    @staticmethod
    def _role_for_capability(capability: str) -> str | None:
        role_map = {
            "coding": "coding",
            "testing": "testing",
            "debugging": "debugging",
            "review": "reviewing",
            "security": "security",
            "documentation": "documentation",
            "planning": "planning",
        }
        return role_map.get(capability)
