from __future__ import annotations

from dataclasses import dataclass

from forge.agents.registry import AgentRegistration, AgentRegistry


@dataclass(frozen=True)
class AgentCandidate:
    registration: AgentRegistration
    score: float


class AgentSelector:
    """Select registered agents using deterministic capability/role matching."""

    def __init__(self, registry: AgentRegistry) -> None:
        self.registry = registry

    def candidates(
        self,
        capability: str,
        role: str | None = None,
    ) -> list[AgentCandidate]:
        registrations = self.registry.get_by_capability(capability)

        if role is not None:
            registrations = [
                registration
                for registration in registrations
                if registration.role == role
            ]

        candidates = [
            AgentCandidate(
                registration=registration,
                score=self._score(registration, capability, role),
            )
            for registration in registrations
        ]

        return sorted(
            candidates,
            key=lambda candidate: (-candidate.score, candidate.registration.name),
        )

    def select(
        self,
        capability: str,
        role: str | None = None,
    ) -> AgentRegistration | None:
        candidates = self.candidates(capability, role)

        if not candidates:
            return None

        return candidates[0].registration

    @staticmethod
    def _score(
        registration: AgentRegistration,
        capability: str,
        role: str | None,
    ) -> float:
        score = 100.0

        if role is not None and registration.role == role:
            score += 25.0

        if capability in registration.capabilities:
            score += 10.0

        return score
