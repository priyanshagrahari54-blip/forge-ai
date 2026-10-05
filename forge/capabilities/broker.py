"""Capability resolution facade.

This is the boundary between Forge's orchestration and the external ecosystem.
It never installs or executes an unverified candidate. When no verified
capability is registered, the caller receives an explicit gap and can decide
whether discovery, adaptation, composition, extension, or a last-resort build
is appropriate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .registry import CapabilityCandidate, CapabilityRegistry, ReuseStrategy


@dataclass(frozen=True)
class CapabilityResolution:
    capability: str
    candidate: CapabilityCandidate | None
    strategy: str
    available: bool
    reason: str
    next_action: str = "none"

    def to_dict(self) -> dict:
        return {
            "capability": self.capability,
            "candidate": self.candidate.to_dict() if self.candidate else None,
            "strategy": self.strategy,
            "available": self.available,
            "reason": self.reason,
            "next_action": self.next_action,
        }


class CapabilityBroker:
    """Resolve requested capabilities without coupling Forge to providers."""

    def __init__(self, registry: CapabilityRegistry) -> None:
        self.registry = registry

    def resolve(self, capability: str) -> CapabilityResolution:
        candidate = self.registry.choose(capability)
        if candidate is not None:
            return CapabilityResolution(
                capability, candidate, candidate.strategy,
                True, "verified capability is available", "execute",
            )
        return CapabilityResolution(
            capability, None, ReuseStrategy.BUILD.value, False,
            "no verified candidate is registered; discover and verify a candidate before execution",
            "discover",
        )

    def resolve_many(self, capabilities: Iterable[str]) -> list[CapabilityResolution]:
        return [self.resolve(capability) for capability in dict.fromkeys(capabilities)]
