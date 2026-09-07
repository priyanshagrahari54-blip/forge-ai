"""Central acceptance decision (A32.10).

Aggregates the mandatory verification gates into a single ``AcceptanceDecision``
so one passing component can never override a failed mandatory gate. Every gate
must pass for ACCEPT; otherwise the result is REJECT/RECOVER.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forge.security.review import ReviewVerdict


@dataclass
class GateOutcome:
    name: str
    passed: bool
    details: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "details": self.details, "evidence": self.evidence}


@dataclass
class AcceptanceDecision:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    test_result: str = "NOT_AVAILABLE"
    review_result: str = "NOT_AVAILABLE"
    security_result: str = "NOT_AVAILABLE"
    build_result: str = "NOT_AVAILABLE"
    benchmark_result: str = "NOT_AVAILABLE"
    permissions_result: str = "NOT_AVAILABLE"
    rollback_result: str = "NOT_AVAILABLE"
    changed_files: list[str] = field(default_factory=list)
    risk_level: str = "NONE"

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "test_result": self.test_result,
            "review_result": self.review_result,
            "security_result": self.security_result,
            "build_result": self.build_result,
            "benchmark_result": self.benchmark_result,
            "permissions_result": self.permissions_result,
            "rollback_result": self.rollback_result,
            "changed_files": list(self.changed_files),
            "risk_level": self.risk_level,
        }


def _risk_level(review_verdict: str, security_findings: int) -> str:
    if review_verdict == ReviewVerdict.BLOCK.value:
        return "CRITICAL"
    if security_findings:
        return "HIGH"
    if review_verdict == ReviewVerdict.REQUEST_CHANGES.value:
        return "MEDIUM"
    return "NONE"


class AcceptanceEngine:
    """Compute the acceptance decision from mandatory gate outcomes."""

    def decide(
        self,
        *,
        tests: GateOutcome,
        build: GateOutcome,
        lint: GateOutcome,
        review: "ReviewDecision | None" = None,
        security: GateOutcome,
        benchmark: GateOutcome | None = None,
        permissions_ok: bool = True,
        rollback_available: bool = True,
        changed_files: list[str] | None = None,
    ) -> AcceptanceDecision:
        changed = list(changed_files or [])
        reasons: list[str] = []
        all_pass = True

        test_passed = tests.passed
        if not test_passed:
            all_pass = False
            reasons.append(f"tests failed: {tests.details}")
        build_passed = build.passed
        if not build_passed:
            all_pass = False
            reasons.append(f"build failed: {build.details}")
        lint_passed = lint.passed
        if not lint_passed:
            all_pass = False
            reasons.append(f"lint/type failed: {lint.details}")

        review_verdict = ReviewVerdict.APPROVE.value
        if review is not None and not review.approved:
            all_pass = False
            review_verdict = review.verdict.value
            reasons.append(f"review {review_verdict}: {review.reason}")

        security_passed = security.passed
        if not security_passed:
            all_pass = False
            reasons.append(f"security failed: {security.details}")

        benchmark_passed = benchmark is None or benchmark.passed
        if not benchmark_passed:
            all_pass = False
            reasons.append(f"benchmark failed: {benchmark.details}")

        if not permissions_ok:
            all_pass = False
            reasons.append("permission gate failed")
        if not rollback_available:
            all_pass = False
            reasons.append("no checkpoint rollback available")

        if not changed:
            all_pass = False
            reasons.append("no changed files to accept")

        security_findings = len((security.evidence or {}).get("findings", []))
        return AcceptanceDecision(
            accepted=all_pass,
            reasons=reasons,
            test_result="PASS" if test_passed else "FAIL",
            review_result=review_verdict,
            security_result="PASS" if security_passed else "FAIL",
            build_result="PASS" if build_passed else "FAIL",
            benchmark_result="PASS" if benchmark_passed else "FAIL",
            permissions_result="PASS" if permissions_ok else "FAIL",
            rollback_result="AVAILABLE" if rollback_available else "UNAVAILABLE",
            changed_files=changed,
            risk_level=_risk_level(review_verdict, security_findings),
        )
