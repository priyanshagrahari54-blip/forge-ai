"""Explicit external capability adoption gates; never installs or executes code."""
from __future__ import annotations
from dataclasses import dataclass, field
from .adoption import AdoptionDecision, AdoptionPolicy, decide
from .registry import CapabilityCandidate

@dataclass(frozen=True)
class AdoptionGate:
    name: str
    passed: bool
    reason: str

@dataclass
class AdoptionResult:
    candidate: CapabilityCandidate | None
    decision: AdoptionDecision
    gates: list[AdoptionGate] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.decision in {AdoptionDecision.ADOPT, AdoptionDecision.ADAPT,
                                 AdoptionDecision.COMPOSE, AdoptionDecision.EXTEND}

class CapabilityAdoptionPipeline:
    def __init__(self, policy: AdoptionPolicy | None = None):
        self.policy=policy or AdoptionPolicy()

    def evaluate(self, candidate: CapabilityCandidate | None) -> AdoptionResult:
        if candidate is None:
            return AdoptionResult(None,AdoptionDecision.BUILD,[AdoptionGate("candidate",False,"missing")])
        gates=[
          AdoptionGate("verification",candidate.verification_status=="verified","candidate verification status"),
          AdoptionGate("license",bool(candidate.license.strip()),"license metadata present"),
          AdoptionGate("security",candidate.security_status in {"verified","reviewed"},"security status"),
          AdoptionGate("quality",candidate.quality_score >= self.policy.min_quality,
                       f"quality >= {self.policy.min_quality:.2f}"),
        ]
        decision=decide(candidate,policy=self.policy)
        return AdoptionResult(candidate,decision,gates,[g.reason for g in gates])

    def require_approval(self, result: AdoptionResult) -> AdoptionResult:
        if not result.approved:
            return result
        # Human/policy approval is deliberately separate from discovery/verification.
        return result
