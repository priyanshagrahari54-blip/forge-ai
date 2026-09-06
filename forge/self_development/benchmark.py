from __future__ import annotations

import subprocess
import sys
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
    """Measure executable outcomes and latency, not just a pytest boolean."""

    def __init__(self, root: str | Path = "."):
        self.root = Path(root).resolve()

    def _run(self, command: list[str]):
        started = time.perf_counter()
        try:
            process = subprocess.run(command, cwd=self.root, text=True, capture_output=True,
                                     timeout=300, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, time.perf_counter() - started, str(exc), -1
        return (
            process.returncode == 0,
            time.perf_counter() - started,
            (process.stdout + process.stderr)[-1000:],
            process.returncode,
        )

    def run_benchmarks(self, *, task_success: bool | None = None,
                       repair_attempts: int = 0, files_changed: list[str] | None = None,
                       model_latency: float = 0.0, rollback_count: int = 0) -> BenchmarkResult:
        started = time.perf_counter()
        tests, test_latency, output, test_code = self._run([sys.executable, "-m", "pytest", "-q"])
        build, build_latency, build_output, build_code = self._run([sys.executable, "-m", "compileall", "-q", "."])
        checks = [tests, build]
        details = {
            "task_success": task_success,
            "test_success": tests,
            "test_latency_seconds": test_latency,
            "test_returncode": test_code,
            "test_output": output,
            "build_success": build,
            "build_latency_seconds": build_latency,
            "build_returncode": build_code,
            "repair_success": bool(task_success and repair_attempts) if task_success is not None else None,
            "repair_attempts": repair_attempts,
            "retry_count": repair_attempts,
            "files_changed": list(files_changed or []),
            "model_latency_seconds": model_latency,
            "rollback_count": rollback_count,
            "benchmark_duration_seconds": time.perf_counter() - started,
        }
        return BenchmarkResult(len(checks), sum(checks), time.perf_counter() - started, details)
