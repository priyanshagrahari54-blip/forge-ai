"""Continuous-autonomy policy for long-running Forge work.

"Unlimited" in Forge means the user may maintain an unbounded backlog and
Forge can continue eligible work without artificial task-count caps. It does
not mean unlimited free compute, provider quotas, network access, or safety
permissions. Resource/provider limits remain hard boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class ExhaustionAction(str, Enum):
    ROTATE = "rotate"
    QUEUE = "queue"
    PAUSE = "pause"
    FAIL = "fail"


@dataclass(frozen=True)
class AutonomyPolicy:
    enabled: bool = True
    unbounded_backlog: bool = True
    auto_retry: bool = True
    auto_replan: bool = True
    auto_provider_rotation: bool = True
    max_consecutive_repairs: int = 5
    max_attempts_per_task: int = 0  # 0 = no artificial task-attempt cap
    stop_on_quota_exhaustion: bool = True
    exhaustion_action: ExhaustionAction = ExhaustionAction.QUEUE
    require_approval_for_risky_actions: bool = True
    require_contract_before_execution: bool = True
    require_verification_before_delivery: bool = True
    preserve_checkpoints: bool = True

    def validate(self) -> None:
        if self.max_consecutive_repairs < 0:
            raise ValueError("max_consecutive_repairs must be >= 0")
        if self.max_attempts_per_task < 0:
            raise ValueError("max_attempts_per_task must be >= 0")


@dataclass
class ProviderBudget:
    provider: str
    available: bool = True
    exhausted: bool = False
    verified: bool = False
    remaining_units: float | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ContinuationDecision:
    action: ExhaustionAction
    reason: str
    provider: str = ""


class AutonomyController:
    """Decide whether eligible work may continue without weakening safety."""

    def __init__(self, policy: AutonomyPolicy | None = None) -> None:
        self.policy = policy or AutonomyPolicy()
        self.policy.validate()

    def can_continue(
        self,
        *,
        contract_ready: bool,
        verification_required: bool,
        verification_complete: bool = False,
        provider: ProviderBudget | None = None,
        risky_action: bool = False,
        approval_granted: bool = False,
    ) -> bool:
        if not self.policy.enabled:
            return False
        if self.policy.require_contract_before_execution and not contract_ready:
            return False
        if verification_required and not verification_complete:
            # Verification is a delivery gate, not a reason to run forever.
            return True
        if risky_action and self.policy.require_approval_for_risky_actions and not approval_granted:
            return False
        if provider is not None and (not provider.available or provider.exhausted):
            return False
        return True

    def exhaustion_decision(self, budgets: Iterable[ProviderBudget]) -> ContinuationDecision:
        available = [b for b in budgets if b.available and not b.exhausted and b.verified]
        if available and self.policy.auto_provider_rotation:
            return ContinuationDecision(
                ExhaustionAction.ROTATE,
                "Current provider unavailable/exhausted; a verified alternative exists.",
                available[0].provider,
            )
        if self.policy.stop_on_quota_exhaustion:
            if self.policy.exhaustion_action == ExhaustionAction.QUEUE:
                return ContinuationDecision(
                    ExhaustionAction.QUEUE,
                    "All eligible providers are exhausted; preserve the task in queue without claiming completion.",
                )
            return ContinuationDecision(
                self.policy.exhaustion_action,
                "No verified provider has remaining capacity.",
            )
        return ContinuationDecision(
            ExhaustionAction.FAIL,
            "No verified provider is available and exhaustion continuation is disabled.",
        )
