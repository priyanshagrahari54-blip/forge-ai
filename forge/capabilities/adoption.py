"""Forge's reuse/adoption policy for external capabilities.

This layer is intentionally not an installer. It converts a verified external
candidate into an explicit adoption decision so Forge can remain its own
system while reusing mature upstream implementations through adapters.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .registry import CapabilityCandidate, ReuseStrategy


class AdoptionDecision(str, Enum):
    ADOPT = "adopt"
    ADAPT = "adapt"
    COMPOSE = "compose"
    EXTEND = "extend"
    BUILD = "build"
    BLOCK = "block"


@dataclass(frozen=True)
class AdoptionPolicy:
    min_quality: float = 0.75
    require_verified_security: bool = True
    require_known_license: bool = True
    allow_unknown_cost: bool = True


def decide(candidate: CapabilityCandidate | None, *, policy: AdoptionPolicy | None = None) -> AdoptionDecision:
    """Choose how Forge should use a candidate; never silently execute it."""
    policy = policy or AdoptionPolicy()
    if candidate is None:
        return AdoptionDecision.BUILD
    if candidate.verification_status != "verified":
        return AdoptionDecision.BLOCK
    if policy.require_verified_security and candidate.security_status not in {"verified", "reviewed"}:
        return AdoptionDecision.BLOCK
    if policy.require_known_license and not candidate.license.strip():
        return AdoptionDecision.BLOCK
    if candidate.quality_score < policy.min_quality:
        return AdoptionDecision.BLOCK
    try:
        return AdoptionDecision(candidate.strategy)
    except ValueError:
        return AdoptionDecision.ADAPT


def rank(candidates: Iterable[CapabilityCandidate], *, policy: AdoptionPolicy | None = None) -> list[CapabilityCandidate]:
    """Rank usable candidates without claiming that ranking is validation."""
    policy = policy or AdoptionPolicy()
    accepted = [c for c in candidates if decide(c, policy=policy) is not AdoptionDecision.BLOCK]
    return sorted(accepted, key=lambda c: (-c.quality_score, c.name))
