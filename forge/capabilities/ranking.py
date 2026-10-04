from __future__ import annotations
from forge.capabilities.registry import CapabilityCandidate

def rank(candidates: list[CapabilityCandidate]) -> list[CapabilityCandidate]:
    return sorted(
        candidates,
        key=lambda c: (c.usable, c.security_status == "verified", c.quality_score, c.name),
        reverse=True,
    )

def best(candidates: list[CapabilityCandidate]) -> CapabilityCandidate | None:
    for candidate in rank(candidates):
        if candidate.usable:
            return candidate
    return None
