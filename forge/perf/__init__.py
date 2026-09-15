"""Performance lab (A83): measure, compare, gate.

Every claim of "faster" must come out of :meth:`Comparison.claims`, which
returns only what the measurements support.
"""
from __future__ import annotations

from forge.perf.lab import (
    BenchmarkResult,
    Comparison,
    MetricComparison,
    PerformanceLab,
    compare,
)
from forge.perf.metrics import (
    ResourceMeter,
    ResourceSample,
    statistics,
)

__all__ = [
    "BenchmarkResult", "Comparison", "MetricComparison", "PerformanceLab",
    "ResourceMeter", "ResourceSample", "compare", "statistics",
]
