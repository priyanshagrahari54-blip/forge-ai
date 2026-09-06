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
    """Deterministically decides whether a candidate improvement is accepted or rejected with explanations."""

    def __init__(self, policy: AcceptancePolicy | None = None) -> None:
        self.policy = policy or AcceptancePolicy()

    def evaluate(self, eval_result: "EvaluationResult") -> Tuple[bool, Optional[str], Optional[str]]:
        """Returns (accepted, accepted_because, rejected_because)."""

        # 1. Reject on build / compilation failure
        if not eval_result.build_ok:
            reason = "Repository build or Python syntax compilation failed"
            return False, None, reason

        # 2. Reject on test regressions
        if eval_result.regressions and not self.policy.allow_test_regressions:
            reason = f"Test regressions detected: {', '.join(eval_result.regressions)}"
            return False, None, reason

        if eval_result.test_delta < 0 and not self.policy.allow_test_regressions:
            reason = f"Passed test count regressed by {abs(eval_result.test_delta)} tests"
            return False, None, reason

        # 3. Reject on security regressions
        if eval_result.security_delta > 0 and not self.policy.allow_security_regressions:
            reason = f"Security vulnerabilities increased by {eval_result.security_delta}"
            return False, None, reason

        # 4. Reject on performance degradation
        if (
            eval_result.performance_delta > self.policy.max_allowed_performance_degradation_sec
        ):
            reason = (
                f"Performance degraded by {eval_result.performance_delta:.2f}s "
                f"(max allowed: {self.policy.max_allowed_performance_degradation_sec:.2f}s)"
            )
            return False, None, reason

        # 5. Class-specific evidence requirement for ACCEPTANCE
        cand_class = (eval_result.candidate_class or "QUALITY_IMPROVEMENT").upper()

        if cand_class == "SECURITY_IMPROVEMENT":
            if eval_result.security_delta >= 0 and not eval_result.improvements:
                return False, None, "Security improvement candidate did not resolve any security findings"

        elif cand_class == "TEST_IMPROVEMENT":
            if eval_result.test_delta <= 0 and not eval_result.improvements:
                return False, None, "Test improvement candidate did not increase test count or fix tests"

        elif cand_class == "PERFORMANCE_IMPROVEMENT":
            if eval_result.performance_delta >= 0.0 and not eval_result.improvements:
                return False, None, "Performance improvement candidate did not show latency reduction"

        # Accepted
        accepted_because = (
            f"Candidate verified successfully ({cand_class}): "
            f"Build OK, no test/security regressions. "
            + (f"Improvements: {', '.join(eval_result.improvements)}" if eval_result.improvements else "No regressions detected.")
        )
        return True, accepted_because, None
