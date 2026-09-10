from forge.self_development import (
    CandidateEvaluator,
)


def test_candidate_evaluator_accepts_improvement():
    evaluator = CandidateEvaluator()

    baseline = {
        "passed_benchmarks": 10,
        "total_benchmarks": 10,
        "duration": 1.0,
        "security_issues": 0,
        "build_ok": True,
    }

    candidate = {
        "passed_benchmarks": 11,
        "total_benchmarks": 11,
        "duration": 0.9,
        "security_issues": 0,
        "build_ok": True,
    }

    res = evaluator.evaluate(baseline, candidate)

    assert res.accepted is True
    assert res.test_delta == 1
    assert res.rejection_reason is None


def test_candidate_evaluator_rejects_regression():
    evaluator = CandidateEvaluator()

    baseline = {
        "passed_benchmarks": 10,
        "total_benchmarks": 10,
        "duration": 1.0,
        "security_issues": 0,
        "build_ok": True,
    }

    candidate_broken = {
        "passed_benchmarks": 8,
        "total_benchmarks": 10,
        "duration": 1.2,
        "security_issues": 1,
        "build_ok": True,
    }

    res = evaluator.evaluate(baseline, candidate_broken)

    assert res.accepted is False
    assert res.rejection_reason is not None
    assert "Test regressions detected" in res.rejection_reason or "vulnerabilities" in res.rejection_reason
