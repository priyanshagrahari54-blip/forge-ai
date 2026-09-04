from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from forge.agents.execution import AgentExecutor


@dataclass(frozen=True)
class AgentRegistration:
    """Metadata describing a registered agent."""

    name: str
    role: str
    executor: AgentExecutor


class AgentRegistry:
    """Central registry for Forge agent executors."""

    def __init__(
        self,
        agents: Iterable[AgentRegistration] | None = None,
    ) -> None:
        self._agents: dict[str, AgentRegistration] = {}

        for registration in agents or ():
            self.register(registration)

    def register(self, registration: AgentRegistration) -> None:
        if not registration.name:
            raise ValueError("Agent name cannot be empty")

        if not registration.role:
            raise ValueError("Agent role cannot be empty")

        if registration.name in self._agents:
            raise ValueError(
                f"Agent already registered: {registration.name}"
            )

        self._agents[registration.name] = registration

    def replace(self, registration: AgentRegistration) -> None:
        if not registration.name:
            raise ValueError("Agent name cannot be empty")

        if not registration.role:
            raise ValueError("Agent role cannot be empty")

        self._agents[registration.name] = registration

    def get(self, name: str) -> AgentRegistration:
        try:
            return self._agents[name]
        except KeyError:
            raise KeyError(f"Unknown agent: {name}") from None

    def get_by_role(self, role: str) -> list[AgentRegistration]:
        return sorted(
            (
                registration
                for registration in self._agents.values()
                if registration.role == role
            ),
            key=lambda registration: registration.name,
        )

    def has(self, name: str) -> bool:
        return name in self._agents

    def remove(self, name: str) -> None:
        if name not in self._agents:
            raise KeyError(f"Unknown agent: {name}")

        del self._agents[name]

    def names(self) -> list[str]:
        return sorted(self._agents)

    def roles(self) -> list[str]:
        return sorted(
            {registration.role for registration in self._agents.values()}
        )

    def __len__(self) -> int:
        return len(self._agents)
