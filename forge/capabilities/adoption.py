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


def verify_candidate(candidate: CapabilityCandidate, *, quality_score: float, security_status: str, license: str | None = None) -> CapabilityCandidate:
    """Promote a discovered candidate only after explicit verification evidence."""
    if not 0.0 <= quality_score <= 1.0:
        raise ValueError("quality_score must be between 0 and 1")
    if security_status not in {"verified", "reviewed"}:
        raise ValueError("candidate security status must be verified or reviewed")
    values = candidate.to_dict()
    values["quality_score"] = quality_score
    values["security_status"] = security_status
    values["verification_status"] = "verified"
    metrics = dict(values.get("metrics") or {})
    metrics["verification_evidence"] = {"quality_score": quality_score, "security_status": security_status, "license_checked": bool((license if license is not None else values.get("license", "")).strip())}
    values["metrics"] = metrics
    if license is not None:
        values["license"] = license
    return CapabilityCandidate(**values)


def rank(candidates: Iterable[CapabilityCandidate], *, policy: AdoptionPolicy | None = None) -> list[CapabilityCandidate]:
    """Rank usable candidates without claiming that ranking is validation."""
    policy = policy or AdoptionPolicy()
    accepted = [c for c in candidates if decide(c, policy=policy) is not AdoptionDecision.BLOCK]
    return sorted(accepted, key=lambda c: (-c.quality_score, c.name))
