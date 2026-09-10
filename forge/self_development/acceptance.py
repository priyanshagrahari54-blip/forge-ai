from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    from forge.self_development.evaluator import EvaluationResult


@dataclass
class AcceptancePolicy:
    allow_test_regressions: bool = False
    allow_security_regressions: bool = False
    max_allowed_performance_degradation_sec: float = 5.0


class AcceptanceEvaluator:
    """Deterministically decides whether a candidate improvement is accepted or rejected."""

    def __init__(self, policy: AcceptancePolicy | None = None) -> None:
        self.policy = policy or AcceptancePolicy()

    def evaluate(self, eval_result: "EvaluationResult") -> Tuple[bool, Optional[str]]:
        if eval_result.regressions and not self.policy.allow_test_regressions:
            reason = f"Test regressions detected: {', '.join(eval_result.regressions)}"
            return False, reason

        if eval_result.test_delta < 0 and not self.policy.allow_test_regressions:
            reason = f"Test count regressed by {abs(eval_result.test_delta)} tests"
            return False, reason

        if eval_result.security_delta > 0 and not self.policy.allow_security_regressions:
            reason = f"Security vulnerabilities increased by {eval_result.security_delta}"
            return False, reason

        if (
            eval_result.performance_delta > self.policy.max_allowed_performance_degradation_sec
        ):
            reason = (
                f"Performance degraded by {eval_result.performance_delta:.2f} seconds "
                f"(max allowed: {self.policy.max_allowed_performance_degradation_sec:.2f}s)"
            )
            return False, reason

        return True, None
