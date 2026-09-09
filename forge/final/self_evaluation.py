"""Final self-evaluation (A77): honest grade from real telemetry.

The grade is computed from recorded facts only — run outcomes,
benchmark results, hardening posture, failure-ledger totals.
Thresholds are deterministic and conservative; the evaluator never
claims capability beyond the evidence.
"""
from __future__ import annotations

import time
from typing import Any

from forge.benchmark.harness import run_benchmark
from forge.security.hardening import run_hardening_report


def self_evaluation(plane: Any, session: Any) -> dict[str, Any]:
    counters = {}
    try:
        counters = plane.observability_snapshot(session)["counters"]
    except Exception:
        pass
    succeeded = int(counters.get("runs.succeeded", 0) or 0)
    failed = int(counters.get("runs.failed", 0) or 0)
    submitted = int(counters.get("tasks.submitted", 0) or 0)
    finished = succeeded + failed
    success_rate = (succeeded / finished) if finished else None

    benchmark = {"passed": 0, "total": 0}
    try:
        _results, summary = run_benchmark(plane.fabric)
        benchmark = {"passed": int(summary["passed"]),
                     "total": int(summary["total"])}
    except Exception:
        pass

    hardening = run_hardening_report(
        plane.policy, plane.sessions, plane.projects)
    secret_hits = sum(len(entry["hits"]) for entry in hardening["secrets"])

    try:
        ledger = plane._failure_ledger().stats()
    except Exception:
        ledger = {}

    findings = {
        "tasks_submitted": submitted,
        "runs_succeeded": succeeded,
        "runs_failed": failed,
        "success_rate": round(success_rate, 3)
        if success_rate is not None else None,
        "benchmark_passed": benchmark["passed"],
        "benchmark_total": benchmark["total"],
        "hardening_overall": hardening["overall"],
        "secret_hits": secret_hits,
        "distinct_failures": ledger.get("distinct_keys", 0),
    }

    if secret_hits:
        grade = "degraded"
    elif finished == 0:
        grade = "unproven"
    elif success_rate is not None and success_rate >= 0.8 and \
            hardening["overall"] == "ok":
        grade = "healthy"
    else:
        grade = "attention"

    return {
        "grade": grade,
        "findings": findings,
        "checked_at": time.time(),
        "note": ("Grades are derived from recorded outcomes and live "
                 "audits; unproven means there is no successful-run "
                 "evidence yet — never a claimed capability."),
    }
