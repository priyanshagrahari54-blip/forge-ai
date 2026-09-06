import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from forge.self_development.acceptance import AcceptanceEvaluator, AcceptancePolicy
from forge.self_development.benchmark import BenchmarkResult, BenchmarkRunner
from forge.self_development.metrics import TestMetrics
from forge.self_development.security import SecurityResult, SecurityScanner
from forge.self_development.verification import BuildVerifier, VerificationResult


@dataclass
class EvaluationResult:
    baseline_score: float = 0.0
    candidate_score: float = 0.0
    candidate_class: str = "MAINTENANCE"
    improvements: List[str] = field(default_factory=list)
    regressions: List[str] = field(default_factory=list)
    security_delta: int = 0
    performance_delta: float = 0.0
    test_delta: int = 0
    build_ok: bool = True
    accepted: bool = False
    accepted_because: Optional[str] = None
    rejection_reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvaluationResult":
        return cls(
            baseline_score=float(data.get("baseline_score", 0.0)),
            candidate_score=float(data.get("candidate_score", 0.0)),
            candidate_class=data.get("candidate_class", "MAINTENANCE"),
            improvements=list(data.get("improvements", [])),
            regressions=list(data.get("regressions", [])),
            security_delta=int(data.get("security_delta", 0)),
            performance_delta=float(data.get("performance_delta", 0.0)),
            test_delta=int(data.get("test_delta", 0)),
            build_ok=bool(data.get("build_ok", True)),
            accepted=bool(data.get("accepted", False)),
            accepted_because=data.get("accepted_because"),
            rejection_reason=data.get("rejection_reason"),
        )


class CandidateEvaluator:
    """Evaluates baseline state vs candidate state using real test, build, security, and benchmark verification."""

    def __init__(
        self,
        root: str | Path = ".",
        acceptance_policy: Optional[AcceptancePolicy] = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.benchmark_runner = BenchmarkRunner(self.root)
        self.security_scanner = SecurityScanner(self.root)
        self.build_verifier = BuildVerifier(self.root)
        self.acceptance_evaluator = AcceptanceEvaluator(acceptance_policy)

    def capture_state(self) -> dict[str, Any]:
        test_metrics = self.benchmark_runner.run_tests()
        bench_avail, bench_results = self.benchmark_runner.run_benchmarks()
        sec_res = self.security_scanner.scan()
        build_res = self.build_verifier.verify_build()

        return {
            "test": test_metrics.to_dict(),
            "benchmarks_available": bench_avail,
            "benchmarks": [b.to_dict() for b in bench_results],
            "security": sec_res.to_dict(),
            "build": build_res.to_dict(),
            "duration": test_metrics.duration,
        }

    def evaluate(
        self,
        baseline_state: dict[str, Any],
        candidate_state: dict[str, Any],
        candidate_class: str = "MAINTENANCE",
    ) -> EvaluationResult:
        improvements: List[str] = []
        regressions: List[str] = []

        base_test = self._get_test_metrics(baseline_state)
        cand_test = self._get_test_metrics(candidate_state)

        base_sec = self._get_security_result(baseline_state)
        cand_sec = self._get_security_result(candidate_state)
        sec_comparison = self.security_scanner.compare(base_sec, cand_sec)

        cand_build = self._get_build_result(candidate_state)

        # Test Delta
        test_delta = cand_test.passed_tests - base_test.passed_tests
        if cand_test.failed_tests > base_test.failed_tests:
            regressions.append(
                f"Failed tests increased from {base_test.failed_tests} to {cand_test.failed_tests}"
            )
        elif cand_test.failed_tests < base_test.failed_tests:
            improvements.append(
                f"Failed tests decreased from {base_test.failed_tests} to {cand_test.failed_tests}"
            )

        if test_delta > 0:
            improvements.append(f"Passed tests increased by {test_delta}")
        elif test_delta < 0:
            regressions.append(f"Passed tests decreased by {abs(test_delta)}")

        # Build Verification
        if not cand_build.passed:
            regressions.append(f"Build/compilation verification failed: {cand_build.stdout or cand_build.stderr}")

        # Security Verification
        sec_delta = len(cand_sec.findings) - len(base_sec.findings)
        if len(sec_comparison.new_findings) > 0:
            regressions.append(f"Introduced {len(sec_comparison.new_findings)} new security findings")
        if len(sec_comparison.resolved_findings) > 0:
            improvements.append(f"Resolved {len(sec_comparison.resolved_findings)} security findings")

        # Performance Delta
        perf_delta = cand_test.duration - base_test.duration
        if perf_delta > 5.0:
            regressions.append(f"Test duration degraded by {perf_delta:.2f}s")
        elif perf_delta < -1.0:
            improvements.append(f"Test duration improved by {abs(perf_delta):.2f}s")

        baseline_score = float(base_test.passed_tests * 10 - len(base_sec.findings) * 20)
        candidate_score = float(cand_test.passed_tests * 10 - len(cand_sec.findings) * 20)

        eval_res = EvaluationResult(
            baseline_score=baseline_score,
            candidate_score=candidate_score,
            candidate_class=candidate_class,
            improvements=improvements,
            regressions=regressions,
            security_delta=sec_delta,
            performance_delta=perf_delta,
            test_delta=test_delta,
            build_ok=cand_build.passed,
        )

        accepted, accepted_because, rejected_because = self.acceptance_evaluator.evaluate(eval_res)
        eval_res.accepted = accepted
        eval_res.accepted_because = accepted_because
        eval_res.rejection_reason = rejected_because

        return eval_res

    def _get_test_metrics(self, state: dict[str, Any]) -> TestMetrics:
        if "test" in state:
            return TestMetrics(**state["test"])
        passed = state.get("passed_benchmarks", 0)
        total = state.get("total_benchmarks", passed)
        failed = max(0, total - passed)
        dur = state.get("duration", 0.0)
        return TestMetrics(total_tests=total, passed_tests=passed, failed_tests=failed, duration=dur)

    def _get_security_result(self, state: dict[str, Any]) -> SecurityResult:
        if "security" in state:
            return SecurityResult(**state["security"])
        sec_issues = state.get("security_issues", 0)
        findings = [{"file": "f", "line": 1, "category": "sec"}] * sec_issues
        return SecurityResult(passed=(sec_issues == 0), findings=findings)

    def _get_build_result(self, state: dict[str, Any]) -> VerificationResult:
        if "build" in state:
            return VerificationResult(**state["build"])
        build_ok = state.get("build_ok", True)
        return VerificationResult(
            command="check",
            exit_code=0 if build_ok else 1,
            duration=0.0,
            stdout="OK" if build_ok else "FAILED",
            stderr="",
            passed=build_ok,
        )
