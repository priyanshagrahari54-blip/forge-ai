"""Final benchmark gate (A74): code-judged model checks as a gate.

Runs the real A60 harness and applies an explicit pass threshold.
No self-graded capabilities: every check is judged by code on the
actual model response.
"""
from __future__ import annotations

import time
from typing import Any

from forge.benchmark.harness import run_benchmark

DEFAULT_MIN_PASSED = 1


def benchmark_gate(plane: Any, session: Any, min_passed: int = 1
                   ) -> dict[str, Any]:
    if min_passed < 0 or min_passed > 8:
        raise ValueError("min_passed must be 0-8")
    results, summary = run_benchmark(plane.fabric)
    passed = int(summary["passed"])
    try:
        names = plane.fabric.registry.names()
    except Exception:
        names = []
    all_benchmarked = summary["models_benchmarked"] >= len(names)
    gate_passed = bool(all_benchmarked) and passed >= min_passed
    return {
        "passed": bool(gate_passed),
        "min_passed": min_passed,
        "benchmarks": results,
        "summary": summary,
        "checked_at": time.time(),
        "all_models_benchmarked": bool(all_benchmarked),
        "note": ("Judged by code on real responses; models never "
                 "grade themselves."),
    }
