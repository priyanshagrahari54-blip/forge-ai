from __future__ import annotations

from dataclasses import dataclass

from forge.agents.registry import AgentRegistration, AgentRegistry
from forge.agents.requirements import TaskRequirementExtractor, TaskRequirements
from forge.agents.selector import AgentSelector


@dataclass(frozen=True)
class PlannedAgent:
    """An agent selected to satisfy one task capability."""

    capability: str
    registration: AgentRegistration
    order: int


@dataclass(frozen=True)
class AgentPlan:
    """Deterministic multi-agent execution plan."""

    requirements: TaskRequirements
    agents: tuple[PlannedAgent, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(agent.registration.name for agent in self.agents)

    @property
    def capabilities(self) -> tuple[str, ...]:
        return tuple(agent.capability for agent in self.agents)

    def is_empty(self) -> bool:
        return not self.agents


class CapabilityAgentPlanner:
    """Build deterministic multi-agent plans from task requirements."""

    def __init__(
        self,
        registry: AgentRegistry,
        extractor: TaskRequirementExtractor | None = None,
        selector: AgentSelector | None = None,
    ) -> None:
        self.registry = registry
        self.extractor = extractor or TaskRequirementExtractor()
        self.selector = selector or AgentSelector(registry)

    def plan(self, task_description: str) -> AgentPlan:
        requirements = self.extractor.extract(task_description)

        selected: list[PlannedAgent] = []
        used_agents: set[str] = set()

        for order, capability in enumerate(requirements.capabilities):
            role = self._role_for_capability(requirements, capability)

            registration = self.selector.select(
                capability,
                role=role,
            )

            if registration is None:
                continue

            # Prefer one agent assignment per capability, but never
            # duplicate the same registered agent in one plan.
            if registration.name in used_agents:
                continue

            used_agents.add(registration.name)

            selected.append(
                PlannedAgent(
                    capability=capability,
                    registration=registration,
                    order=order,
                )
            )

        return AgentPlan(
            requirements=requirements,
            agents=tuple(selected),
        )

    @staticmethod
    def _role_for_capability(
        requirements: TaskRequirements,
        capability: str,
    ) -> str | None:
        for index, requirement_capability in enumerate(
            requirements.capabilities
        ):
            if requirement_capability != capability:
                continue

            if index < len(requirements.roles):
                return requirements.roles[index]

            return None

        return None
