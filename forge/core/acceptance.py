"""Central acceptance decision (A32.7 / A32.10).

Aggregates the mandatory verification gates into a single ``AcceptanceDecision``
so one passing component can never override a failed mandatory gate. Every gate
must pass for ACCEPT; otherwise the result is REJECT/RECOVER.

The decision names every failed gate (``failed_gates``) and carries measured
``metrics`` (finding counts, file counts, execution evidence). Gates that did
not execute are never silently converted into success: an unconfigured
lint/type checker passes only under the explicit, documented
pass-when-unconfigured policy, and the omission is recorded in
``metrics["lint_executed"]``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forge.security.review import ReviewDecision, ReviewVerdict


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
    failed_gates: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

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
            "failed_gates": list(self.failed_gates),
            "metrics": dict(self.metrics),
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
        failed_gates: list[str] = []
        all_pass = True

        def fail(gate: str, reason: str) -> None:
            nonlocal all_pass
            all_pass = False
            failed_gates.append(gate)
            reasons.append(reason)

        test_passed = tests.passed
        if not test_passed:
            fail("tests", f"tests failed: {tests.details}")
        build_passed = build.passed
        if not build_passed:
            fail("build", f"build failed: {build.details}")
        lint_passed = lint.passed
        if not lint_passed:
            fail("lint", f"lint/type failed: {lint.details}")

        review_verdict = ReviewVerdict.APPROVE.value
        if review is not None and not review.approved:
            review_verdict = review.verdict.value
            fail("review", f"review {review_verdict}: {review.reason}")

        security_passed = security.passed
        if not security_passed:
            fail("security", f"security failed: {security.details}")

        benchmark_passed = benchmark is None or benchmark.passed
        if not benchmark_passed:
            fail("benchmark", f"benchmark failed: {benchmark.details}")

        if not permissions_ok:
            fail("permissions", "permission gate failed")
        if not rollback_available:
            fail("rollback", "no checkpoint rollback available")

        if not changed:
            fail("changes", "no changed files to accept")

        security_findings = len((security.evidence or {}).get("findings", []))
        review_findings = len(review.findings) if review is not None else 0
        metrics: dict[str, Any] = {
            "changed_file_count": len(changed),
            "security_findings": security_findings,
            "review_findings": review_findings,
            "review_verdict": review_verdict,
            # Explicit execution evidence: a lint gate with no configured
            # checker passes only under the documented pass-when-unconfigured
            # policy, and the omission is visible here.
            "lint_executed": bool((lint.evidence or {}).get("commands")),
            "tests_no_tests": bool((tests.evidence or {}).get("no_tests", False)),
        }
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
            failed_gates=failed_gates,
            metrics=metrics,
        )
