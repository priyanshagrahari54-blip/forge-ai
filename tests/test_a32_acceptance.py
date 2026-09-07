"""Central acceptance engine tests (A32.10)."""
from forge.core.acceptance import AcceptanceEngine, GateOutcome
from forge.security.review import FindingSeverity, ReviewDecision, ReviewFinding, ReviewVerdict


def _pass(name):
    return GateOutcome(name, True, "")


def _fail(name, details="failed"):
    return GateOutcome(name, False, details)


def _review(verdict, findings=()):
    return ReviewDecision(verdict=verdict, findings=list(findings), reason="test")


def test_all_gates_pass_accepts():
    decision = AcceptanceEngine().decide(
        tests=_pass("tests"), build=_pass("build"), lint=_pass("lint"),
        review=_review(ReviewVerdict.APPROVE), security=_pass("security"),
        changed_files=["app.py"],
    )
    assert decision.accepted
    assert decision.test_result == "PASS"
    assert decision.risk_level == "NONE"


def test_failed_test_gate_blocks():
    decision = AcceptanceEngine().decide(
        tests=_fail("tests"), build=_pass("build"), lint=_pass("lint"),
        review=_review(ReviewVerdict.APPROVE), security=_pass("security"),
        changed_files=["app.py"],
    )
    assert not decision.accepted
    assert any("tests failed" in reason for reason in decision.reasons)


def test_blocked_review_blocks_acceptance():
    decision = AcceptanceEngine().decide(
        tests=_pass("tests"), build=_pass("build"), lint=_pass("lint"),
        review=_review(ReviewVerdict.BLOCK, [ReviewFinding(FindingSeverity.CRITICAL, "boom")]),
        security=_pass("security"), changed_files=["app.py"],
    )
    assert not decision.accepted
    assert decision.review_result == "BLOCK"
    assert decision.risk_level == "CRITICAL"


def test_failed_security_blocks_acceptance():
    security = GateOutcome("security", False, "found a secret", {"findings": [{"file": "app.py"}]})
    decision = AcceptanceEngine().decide(
        tests=_pass("tests"), build=_pass("build"), lint=_pass("lint"),
        review=_review(ReviewVerdict.APPROVE), security=security, changed_files=["app.py"],
    )
    assert not decision.accepted
    assert decision.security_result == "FAIL"
    assert decision.risk_level == "HIGH"


def test_one_passing_gate_cannot_override_failure():
    decision = AcceptanceEngine().decide(
        tests=_fail("tests"), build=_pass("build"), lint=_pass("lint"),
        review=_review(ReviewVerdict.APPROVE), security=_pass("security"),
        changed_files=["app.py"],
    )
    assert not decision.accepted


def test_no_changed_files_rejects():
    decision = AcceptanceEngine().decide(
        tests=_pass("tests"), build=_pass("build"), lint=_pass("lint"),
        review=_review(ReviewVerdict.APPROVE), security=_pass("security"),
        changed_files=[],
    )
    assert not decision.accepted
    assert any("no changed files" in reason for reason in decision.reasons)


def test_no_rollback_rejects():
    decision = AcceptanceEngine().decide(
        tests=_pass("tests"), build=_pass("build"), lint=_pass("lint"),
        review=_review(ReviewVerdict.APPROVE), security=_pass("security"),
        rollback_available=False, changed_files=["app.py"],
    )
    assert not decision.accepted
    assert decision.rollback_result == "UNAVAILABLE"


def test_benchmark_gate_blocks_when_incomplete():
    decision = AcceptanceEngine().decide(
        tests=_pass("tests"), build=_pass("build"), lint=_pass("lint"),
        review=_review(ReviewVerdict.APPROVE), security=_pass("security"),
        benchmark=GateOutcome("benchmark", False, "incomplete"), changed_files=["app.py"],
    )
    assert not decision.accepted
