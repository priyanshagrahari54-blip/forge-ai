from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from forge.runtime.runtime import ToolResult


@dataclass
class VerificationResult:
    success: bool
    stage: str
    findings: list[str] = field(default_factory=list)
    score: float = 1.0
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class SecurityGate:
    """Security verification gate checking for credential leaks and dangerous calls."""

    DANGEROUS_PATTERNS = (
        ("eval(", "Use of eval() detected."),
        ("os.system(", "Use of os.system() detected."),
        ("subprocess.Popen(", "Raw subprocess.Popen call detected."),
        ("OPENAI_API_KEY =", "Hardcoded OpenAI API key detected."),
        ("AWS_SECRET_ACCESS_KEY =", "Hardcoded AWS secret detected."),
        ("-----BEGIN PRIVATE KEY-----", "Hardcoded private key detected."),
    )

    def verify(self, changes: dict[str, str]) -> VerificationResult:
        findings: list[str] = []
        errors: list[str] = []

        for path, content in changes.items():
            for pattern, msg in self.DANGEROUS_PATTERNS:
                if pattern in content:
                    errors.append(f"Security violation in {path}: {msg}")

        success = len(errors) == 0
        return VerificationResult(
            success=success,
            stage="security",
            findings=findings,
            score=1.0 if success else 0.0,
            errors=errors,
        )


class ReviewGate:
    """Review verification gate evaluating code quality and structure."""

    def verify(self, changes: dict[str, str], diff: str = "") -> VerificationResult:
        findings: list[str] = []
        errors: list[str] = []

        for path, content in changes.items():
            if "REJECT_REVIEW" in content or "CRITICAL_REVIEW_FAIL" in content:
                errors.append(f"Review rejection marker found in {path}")
            elif "TODO" in content or "FIXME" in content:
                findings.append(f"Note: Pending TODO/FIXME in {path}")

        success = len(errors) == 0
        return VerificationResult(
            success=success,
            stage="review",
            findings=findings,
            score=1.0 if success else 0.0,
            errors=errors,
        )


class VerificationPipeline:
    """Complete verification pipeline (Tests -> Review -> Security -> Acceptance)."""

    def __init__(self) -> None:
        self.security_gate = SecurityGate()
        self.review_gate = ReviewGate()

    def verify_candidate(
        self,
        changes: dict[str, str],
        test_result: ToolResult | None = None,
        diff: str = "",
    ) -> list[VerificationResult]:
        results: list[VerificationResult] = []

        # 1. Test Gate
        test_success = test_result.success if test_result else True
        test_errors = [test_result.error] if test_result and test_result.error else []
        results.append(
            VerificationResult(
                success=test_success,
                stage="tests",
                findings=[test_result.output] if test_result and test_result.output else [],
                score=1.0 if test_success else 0.0,
                errors=test_errors,
            )
        )

        # 2. Review Gate
        review_res = self.review_gate.verify(changes, diff=diff)
        results.append(review_res)

        # 3. Security Gate
        security_res = self.security_gate.verify(changes)
        results.append(security_res)

        # 4. Acceptance Gate
        all_passed = all(r.success for r in results)
        results.append(
            VerificationResult(
                success=all_passed,
                stage="acceptance",
                findings=["All gates passed"] if all_passed else ["Candidate rejected by verification gates"],
                score=1.0 if all_passed else 0.0,
                errors=[] if all_passed else ["Acceptance check failed due to preceding gate failures"],
            )
        )

        return results
