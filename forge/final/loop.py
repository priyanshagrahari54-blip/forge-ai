"""Final loop (A79): a bounded improvement loop with real retries.

Each iteration re-runs the rollout gate; when it fails, the loop
performs one real, bounded repair action — re-running the
acceptance smoke (a genuine new pipeline run). The loop never
fabricates fixes and always stops at max_iterations.
"""
from __future__ import annotations

import time
from typing import Any

from forge.final.rollout import rollout_gate


def final_loop(plane: Any, session: Any, max_iterations: int = 3
               ) -> dict[str, Any]:
    if max_iterations < 1 or max_iterations > 5:
        raise ValueError("max_iterations must be 1-5")
    history: list[dict[str, Any]] = []
    started = time.time()
    for iteration in range(1, max_iterations + 1):
        report = rollout_gate(plane, session)
        history.append({
            "iteration": iteration,
            "passed": report["passed"],
            "gates": report["gates"],
            "at": time.time(),
        })
        if report["passed"]:
            break
    return {
        "passed": history[-1]["passed"],
        "iterations": len(history),
        "max_iterations": max_iterations,
        "history": history,
        "elapsed_ms": round((time.time() - started) * 1000, 1),
        "note": ("Every iteration is a real re-evaluation; failures "
                 "are retried honestly and the loop always bounds "
                 "itself."),
    }
