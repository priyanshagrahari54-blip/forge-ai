import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from forge.self_development.acceptance import AcceptanceEvaluator, AcceptancePolicy
from forge.self_development.benchmark import BenchmarkRunner


@dataclass
class EvaluationResult:
    baseline_score: float = 0.0
    candidate_score: float = 0.0
    improvements: list[str] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    security_delta: int = 0
    performance_delta: float = 0.0
    test_delta: int = 0
    accepted: bool = False
    rejection_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvaluationResult":
        return cls(
            baseline_score=float(data.get("baseline_score", 0.0)),
            candidate_score=float(data.get("candidate_score", 0.0)),
            improvements=list(data.get("improvements", [])),
            regressions=list(data.get("regressions", [])),
            security_delta=int(data.get("security_delta", 0)),
            performance_delta=float(data.get("performance_delta", 0.0)),
            test_delta=int(data.get("test_delta", 0)),
            accepted=bool(data.get("accepted", False)),
            rejection_reason=data.get("rejection_reason"),
        )


class CandidateEvaluator:
    """Evaluates candidate against baseline checks and determines acceptance."""

    def __init__(
        self,
        root: str | Path = ".",
        acceptance_policy: AcceptancePolicy | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.benchmark_runner = BenchmarkRunner(self.root)
        self.acceptance_evaluator = AcceptanceEvaluator(acceptance_policy)

    def capture_state(self) -> dict[str, Any]:
        bench = self.benchmark_runner.run_benchmarks()
        sec_issues = self._check_security()
        build_ok = self._check_build()

        return {
            "passed_benchmarks": bench.passed_benchmarks,
            "total_benchmarks": bench.total_benchmarks,
            "duration": bench.duration,
            "security_issues": sec_issues,
            "build_ok": build_ok,
        }

    def evaluate(
        self, baseline_state: dict[str, Any], candidate_state: dict[str, Any],
        *, require_improvement: bool = False,
    ) -> EvaluationResult:
        improvements: list[str] = []
        regressions: list[str] = []

        baseline_bench = baseline_state.get("passed_benchmarks", 0)
        candidate_bench = candidate_state.get("passed_benchmarks", 0)

        test_delta = candidate_bench - baseline_bench

        if candidate_bench < baseline_bench:
            regressions.append(f"Benchmark tests decreased from {baseline_bench} to {candidate_bench}")
        elif candidate_bench > baseline_bench:
            improvements.append(f"Benchmark tests increased from {baseline_bench} to {candidate_bench}")

        if not candidate_state.get("build_ok", True) and baseline_state.get("build_ok", True):
            regressions.append("Candidate failed repository build check")

        baseline_sec = baseline_state.get("security_issues", 0)
        candidate_sec = candidate_state.get("security_issues", 0)
        security_delta = candidate_sec - baseline_sec

        if security_delta > 0:
            regressions.append(f"Security vulnerabilities increased by {security_delta}")
        elif security_delta < 0:
            improvements.append(f"Security vulnerabilities reduced by {abs(security_delta)}")

        perf_delta = candidate_state.get("duration", 0.0) - baseline_state.get("duration", 0.0)

        baseline_score = float(baseline_bench * 10 - baseline_sec * 20)
        candidate_score = float(candidate_bench * 10 - candidate_sec * 20)
        if require_improvement and candidate_score <= baseline_score:
            regressions.append("No measurable improvement over the baseline")

        eval_res = EvaluationResult(
            baseline_score=baseline_score,
            candidate_score=candidate_score,
            improvements=improvements,
            regressions=regressions,
            security_delta=security_delta,
            performance_delta=perf_delta,
            test_delta=test_delta,
        )

        accepted, reason = self.acceptance_evaluator.evaluate(eval_res)
        eval_res.accepted = accepted
        eval_res.rejection_reason = reason

        return eval_res

    def _check_security(self) -> int:
        # Count concrete high-risk findings; never use a synthetic constant.
        import re
        from forge.security.verification import is_excluded
        patterns = [re.compile(r"(?:api[_-]?key|secret|password)\\s*[:=]\\s*['\\\"][^'\\\"]{8,}", re.I), re.compile(r"-----BEGIN .*PRIVATE KEY-----")]
        count = 0
        for path in self.root.rglob("*"):
            if not path.is_file() or is_excluded(path.relative_to(self.root).parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            count += sum(1 for pattern in patterns if pattern.search(text))
        return count

    def _check_build(self) -> bool:
        import sys
        proc = subprocess.run([sys.executable, "-m", "compileall", "-q", "."], cwd=self.root, capture_output=True, check=False)
        return proc.returncode == 0
