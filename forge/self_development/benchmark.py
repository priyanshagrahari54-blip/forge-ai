import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from forge.self_development.metrics import TestMetrics


@dataclass
class BenchmarkDefinition:
    name: str
    command_or_fn: Any
    timeout: float = 30.0
    metric: str = "duration_sec"
    threshold: float = 10.0


@dataclass
class BenchmarkResult:
    name: str
    passed: bool
    duration: float
    metrics: Dict[str, float] = field(default_factory=dict)
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BenchmarkRunner:
    """Executes pytest for test metrics and runs registered performance benchmarks."""

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()
        self.benchmarks: List[BenchmarkDefinition] = []

    def register_benchmark(self, benchmark: BenchmarkDefinition) -> None:
        self.benchmarks.append(benchmark)

    def run_tests(self) -> TestMetrics:
        start_time = time.time()
        env = dict(os.environ)
        env["PYTHONPATH"] = f"{str(self.root)}:{env.get('PYTHONPATH', '')}"

        pytest_proc = subprocess.run(
            ["pytest", "-q"],
            cwd=str(self.root),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        duration = time.time() - start_time
        stdout = pytest_proc.stdout or ""
        stderr = pytest_proc.stderr or ""

        total, passed, failed, skipped, errors = self._parse_pytest_output(
            stdout, pytest_proc.returncode
        )

        return TestMetrics(
            total_tests=total,
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
            error_tests=errors,
            duration=duration,
            raw_stdout=stdout,
            raw_stderr=stderr,
        )

    def run_benchmarks(self) -> tuple[bool, List[BenchmarkResult]]:
        if not self.benchmarks:
            return False, []

        results: List[BenchmarkResult] = []
        for b in self.benchmarks:
            res = self._run_single_benchmark(b)
            results.append(res)

        return True, results

    def _run_single_benchmark(self, b: BenchmarkDefinition) -> BenchmarkResult:
        start = time.time()
        try:
            if callable(b.command_or_fn):
                b.command_or_fn()
                dur = time.time() - start
                passed = dur <= b.threshold
                return BenchmarkResult(
                    name=b.name,
                    passed=passed,
                    duration=dur,
                    metrics={b.metric: dur},
                )
            elif isinstance(b.command_or_fn, (list, str)):
                cmd = (
                    b.command_or_fn
                    if isinstance(b.command_or_fn, list)
                    else b.command_or_fn.split()
                )
                proc = subprocess.run(
                    cmd,
                    cwd=str(self.root),
                    text=True,
                    capture_output=True,
                    timeout=b.timeout,
                    check=False,
                )
                dur = time.time() - start
                passed = proc.returncode == 0 and dur <= b.threshold
                return BenchmarkResult(
                    name=b.name,
                    passed=passed,
                    duration=dur,
                    metrics={b.metric: dur},
                    details={"stdout": proc.stdout, "stderr": proc.stderr},
                )
        except Exception as exc:
            dur = time.time() - start
            return BenchmarkResult(
                name=b.name,
                passed=False,
                duration=dur,
                details={"error": str(exc)},
            )

        return BenchmarkResult(name=b.name, passed=False, duration=0.0)

    def _parse_pytest_output(
        self, stdout: str, returncode: int
    ) -> tuple[int, int, int, int, int]:
        passed_m = re.search(r"(\d+)\s+passed", stdout)
        failed_m = re.search(r"(\d+)\s+failed", stdout)
        skipped_m = re.search(r"(\d+)\s+skipped", stdout)
        error_m = re.search(r"(\d+)\s+error", stdout)

        passed = int(passed_m.group(1)) if passed_m else 0
        failed = int(failed_m.group(1)) if failed_m else 0
        skipped = int(skipped_m.group(1)) if skipped_m else 0
        errors = int(error_m.group(1)) if error_m else 0

        if returncode == 0 and passed == 0 and "passed" in stdout:
            passed = 1

        total = passed + failed + skipped + errors
        if total == 0 and returncode != 0:
            failed = 1
            total = 1

        return total, passed, failed, skipped, errors
