import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class BenchmarkResult:
    total_benchmarks: int = 0
    passed_benchmarks: int = 0
    duration: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BenchmarkRunner:
    """Executes reproducible test and performance benchmarks on the repository."""

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()

    def run_benchmarks(self) -> BenchmarkResult:
        start_time = time.time()
        details: dict[str, Any] = {}

        env = dict(os.environ)
        env["PYTHONPATH"] = f"{str(self.root)}:{env.get('PYTHONPATH', '')}"

        # Run pytest benchmark
        pytest_proc = subprocess.run(
            ["pytest", "-q"],
            cwd=str(self.root),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        duration = time.time() - start_time
        test_success = pytest_proc.returncode == 0

        details["pytest_returncode"] = pytest_proc.returncode
        details["pytest_stdout"] = pytest_proc.stdout[-500:] if pytest_proc.stdout else ""
        details["pytest_stderr"] = pytest_proc.stderr[-500:] if pytest_proc.stderr else ""

        total = 1
        passed = 1 if test_success else 0

        return BenchmarkResult(
            total_benchmarks=total,
            passed_benchmarks=passed,
            duration=duration,
            details=details,
        )
