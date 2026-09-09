"""Plane metrics (A62): counters and bounded latency reservoirs.

Metrics are recorded at the existing pipeline funnels (guarded so a
metrics error can never break a run) and snapshots are computed from
real events only. Counters live for the lifetime of the plane;
durations keep a bounded reservoir (most recent 100 observations).
"""
from __future__ import annotations

import threading
from typing import Any

RESERVOIR = 100


class MetricsRegistry:
    """Thread-safe counters and bounded duration reservoirs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._durations: dict[str, list[float]] = {}

    def incr(self, name: str, delta: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + delta

    def observe(self, name: str, seconds: float) -> None:
        if seconds < 0:
            seconds = 0.0
        with self._lock:
            reservoir = self._durations.setdefault(name, [])
            reservoir.append(seconds)
            if len(reservoir) > RESERVOIR:
                del reservoir[:len(reservoir) - RESERVOIR]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(self._counters)
            durations = {name: list(samples)
                         for name, samples in self._durations.items()}
        latencies: dict[str, Any] = {}
        for name, samples in durations.items():
            ordered = sorted(samples)
            count = len(ordered)

            def percentile(ratio: float,
                            ordered=ordered,
                            count=count) -> float:
                if not ordered:
                    return 0.0
                index = min(count - 1, int(ratio * count))
                return ordered[index]

            latencies[name] = {
                "count": count,
                "mean_ms": round(
                    (sum(ordered) / count * 1000) if count else 0.0, 1),
                "p50_ms": round(percentile(0.50) * 1000, 1),
                "p95_ms": round(percentile(0.95) * 1000, 1),
                "max_ms": round(ordered[-1] * 1000, 1) if count else 0.0,
            }
        return {"counters": counters, "latencies": latencies}
