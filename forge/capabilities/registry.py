"""Verified capability candidates and reuse strategy."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Iterable

class ReuseStrategy(str, Enum):
    REUSE = "reuse"
    ADAPT = "adapt"
    COMPOSE = "compose"
    EXTEND = "extend"
    BUILD = "build"

@dataclass
class CapabilityCandidate:
    capability: str
    name: str
    source: str
    interface: str
    license: str = ""
    version: str = ""
    dependencies: list[str] = field(default_factory=list)
    security_status: str = "unverified"
    quality_score: float = 0.0
    compatibility: list[str] = field(default_factory=list)
    cost: str = "unknown"
    offline: bool = False
    verification_status: str = "unverified"
    strategy: str = ReuseStrategy.REUSE.value

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def usable(self) -> bool:
        return self.verification_status == "verified" and self.security_status in {"verified", "reviewed"}

class CapabilityRegistry:
    """Small registry; persistence/discovery/install are separate adapters."""
    def __init__(self, candidates: Iterable[CapabilityCandidate] = ()) -> None:
        self._items: dict[tuple[str, str], CapabilityCandidate] = {}
        for candidate in candidates:
            self.register(candidate)

    def register(self, candidate: CapabilityCandidate) -> None:
        if not candidate.capability.strip() or not candidate.name.strip():
            raise ValueError("capability and candidate name are required")
        if not candidate.source.strip():
            raise ValueError("candidate source is required")
        if not 0.0 <= candidate.quality_score <= 1.0:
            raise ValueError("quality_score must be between 0 and 1")
        self._items[(candidate.capability, candidate.name)] = candidate

    def find(self, capability: str, *, usable_only: bool = False) -> list[CapabilityCandidate]:
        items = [item for (key, _), item in self._items.items() if key == capability]
        if usable_only:
            items = [item for item in items if item.usable]
        return sorted(items, key=lambda item: (-item.quality_score, item.name))

    def choose(self, capability: str) -> CapabilityCandidate | None:
        candidates = self.find(capability, usable_only=True)
        return candidates[0] if candidates else None

    def promote(self, candidate: CapabilityCandidate) -> CapabilityCandidate:
        """Register only a candidate that already passed verification gates."""
        if not candidate.usable:
            raise ValueError("only verified and security-reviewed candidates can be promoted")
        self.register(candidate)
        return candidate

    def missing(self, capabilities: Iterable[str]) -> list[str]:
        return [name for name in capabilities if self.choose(name) is None]

    def all(self) -> list[CapabilityCandidate]:
        return sorted(self._items.values(), key=lambda item: (item.capability, item.name))

    def to_dict(self) -> list[dict]:
        return [item.to_dict() for item in self.all()]
