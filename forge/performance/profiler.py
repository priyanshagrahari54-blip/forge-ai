"""Run performance (A63): real timings, honest aggregates.

Profiles derive from the run record's own timestamps (created,
started, finished) — nothing is estimated or invented. Aggregates
are computed over the most recent runs and always bounded.
"""
from __future__ import annotations

from typing import Any, Iterable

MAX_SUMMARY_RUNS = 200
MAX_SLOWEST = 5


def run_profile(run: Any) -> dict[str, Any]:
    finished = run.finished_at is not None
    queue_ms: float | None = None
    execution_ms: float | None = None
    total_ms: float | None = None
    if run.started_at is not None:
        queue_ms = round((run.started_at - run.created_at) * 1000, 1)
    if finished and run.started_at is not None:
        execution_ms = round(
            (run.finished_at - run.started_at) * 1000, 1)
        total_ms = round((run.finished_at - run.created_at) * 1000, 1)
    return {
        "run_id": run.id,
        "status": str(run.status),
        "mode": run.mode,
        "created_at": run.created_at,
        "finished": finished,
        "queue_ms": queue_ms,
        "execution_ms": execution_ms,
        "total_ms": total_ms,
    }


def _stats(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    count = len(ordered)
    if not count:
        return {"count": 0, "mean_ms": 0.0, "median_ms": 0.0,
                "p95_ms": 0.0, "max_ms": 0.0}
    return {
        "count": count,
        "mean_ms": round(sum(ordered) / count, 1),
        "median_ms": round(ordered[count // 2], 1),
        "p95_ms": round(ordered[min(count - 1, int(0.95 * count))], 1),
        "max_ms": round(ordered[-1], 1),
    }


def performance_summary(runs: Iterable[Any]) -> dict[str, Any]:
    rows = list(runs)[:MAX_SUMMARY_RUNS]
    profiles = [run_profile(run) for run in rows]
    terminal = [profile for profile in profiles if profile["finished"]]
    queue = [profile["queue_ms"] for profile in terminal
             if profile["queue_ms"] is not None]
    execution = [profile["execution_ms"] for profile in terminal
                 if profile["execution_ms"] is not None]
    total = [profile["total_ms"] for profile in terminal
             if profile["total_ms"] is not None]
    by_mode: dict[str, int] = {}
    for profile in terminal:
        by_mode[profile["mode"]] = by_mode.get(profile["mode"], 0) + 1
    slowest = sorted(terminal, key=lambda item: item["execution_ms"] or 0,
                     reverse=True)[:MAX_SLOWEST]
    return {
        "runs_considered": len(profiles),
        "terminal_runs": len(terminal),
        "queue_ms": _stats(queue),
        "execution_ms": _stats(execution),
        "total_ms": _stats(total),
        "runs_by_mode": by_mode,
        "slowest_runs": slowest,
    }
