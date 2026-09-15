"""Performance lab (A83): measure, compare, and gate.

The lab exists to make one sentence impossible to say without evidence:
*"this is faster"*. Its rules:

* A :class:`BenchmarkResult` is a set of real samples plus their statistics.
  It never contains an estimate.
* A :class:`Comparison` needs at least ``min_runs`` samples on both sides, or
  it reports ``insufficient_data`` rather than a verdict.
* A metric is only called ``improved`` or ``regression`` when the measured
  delta exceeds the configured tolerance — inside tolerance is ``unchanged``,
  because a 0.4 % wobble on three runs is noise, not a result.
* ``claims`` returns exactly the statements the data supports, phrased with
  the numbers attached. If the data supports nothing, it returns nothing.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from forge.perf.metrics import (
    ResourceMeter,
    ResourceSample,
    statistics,
)

#: Metric name -> (extractor, unit, lower_is_better)
METRICS: Dict[str, Tuple[str, str, bool]] = {
    "wall_ms": ("wall_ms", "ms", True),
    "cpu_seconds": ("cpu_seconds", "s", True),
    "peak_rss_mib": ("peak_rss_mib", "MiB", True),
    "disk_read_bytes": ("disk_read_bytes", "bytes", True),
    "disk_write_bytes": ("disk_write_bytes", "bytes", True),
}
IMPROVED = "improved"
UNCHANGED = "unchanged"
REGRESSION = "regression"
INSUFFICIENT_DATA = "insufficient_data"
NOT_MEASURED = "not_measured"
VERDICTS = (IMPROVED, UNCHANGED, REGRESSION, INSUFFICIENT_DATA, NOT_MEASURED)


@dataclass
class BenchmarkResult:
    """Measured performance of one command or configuration."""

    name: str
    samples: List[ResourceSample] = field(default_factory=list)
    argv: Tuple[str, ...] = ()
    label: str = ""
    created_at: float = field(default_factory=time.time)
    #: Anything the caller measured itself (throughput, frame time, …).
    custom: Dict[str, List[float]] = field(default_factory=dict)
    note: str = ""

    @property
    def runs(self) -> int:
        return len(self.samples)

    @property
    def succeeded(self) -> bool:
        return bool(self.samples) and all(
            sample.status == "succeeded" for sample in self.samples)

    def values(self, metric: str) -> List[float]:
        """Measured values for a metric, skipping samples that lack it."""
        if metric in self.custom:
            return [float(value) for value in self.custom[metric]]
        out: List[float] = []
        for sample in self.samples:
            payload = sample.to_dict()
            value = payload.get(metric)
            if value is not None:
                out.append(float(value))
        return out

    def stats(self) -> Dict[str, Any]:
        return {metric: statistics(self.values(metric)) for metric in METRICS}

    def summary(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "runs": self.runs,
            "succeeded": self.succeeded,
            "argv": list(self.argv),
            "note": self.note,
            "stats": self.stats(),
            "custom": {key: statistics(values)
                       for key, values in sorted(self.custom.items())},
            "sources": sorted({source for sample in self.samples
                               for source in sample.sources}),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {"summary": self.summary(),
                "samples": [sample.to_dict() for sample in self.samples]}


@dataclass
class MetricComparison:
    """One metric, before vs after, with a verdict."""

    metric: str
    unit: str
    lower_is_better: bool
    before: Dict[str, Any]
    after: Dict[str, Any]
    delta: Optional[float]
    delta_percent: Optional[float]
    verdict: str
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric, "unit": self.unit,
            "lower_is_better": self.lower_is_better,
            "before": self.before, "after": self.after,
            "delta": None if self.delta is None else round(self.delta, 3),
            "delta_percent": (None if self.delta_percent is None
                              else round(self.delta_percent, 2)),
            "verdict": self.verdict, "reason": self.reason,
        }


@dataclass
class Comparison:
    """A before/after performance comparison with an overall verdict."""

    before: BenchmarkResult
    after: BenchmarkResult
    metrics: List[MetricComparison] = field(default_factory=list)
    #: Relative change that must be exceeded to call a metric changed.
    tolerance_percent: float = 2.0
    min_runs: int = 3

    @property
    def regressions(self) -> List[MetricComparison]:
        return [item for item in self.metrics if item.verdict == REGRESSION]

    @property
    def improvements(self) -> List[MetricComparison]:
        return [item for item in self.metrics if item.verdict == IMPROVED]

    @property
    def ok(self) -> bool:
        """No measured regression, and enough data to say so."""
        if any(item.verdict == INSUFFICIENT_DATA for item in self.metrics
               if item.metric == "wall_ms"):
            return False
        return not self.regressions

    @property
    def verdict(self) -> str:
        if any(item.verdict == INSUFFICIENT_DATA for item in self.metrics
               if item.metric == "wall_ms"):
            return INSUFFICIENT_DATA
        if self.regressions:
            return REGRESSION
        if self.improvements:
            return IMPROVED
        return UNCHANGED

    def claims(self) -> List[str]:
        """Only the statements the measurements support, numbers attached."""
        out: List[str] = []
        for item in self.metrics:
            if item.verdict not in (IMPROVED, REGRESSION):
                continue
            timing = item.metric in ("wall_ms", "cpu_seconds")
            if item.verdict == IMPROVED:
                direction = "faster" if timing else (
                    "higher" if not item.lower_is_better else "lower")
            else:
                direction = "slower" if timing else (
                    "lower" if not item.lower_is_better else "higher")
            out.append(
                "%s %s: %s -> %s %s (%+.1f%%, %d runs each)" % (
                    item.metric, direction,
                    _fmt(item.before.get("median")),
                    _fmt(item.after.get("median")), item.unit,
                    item.delta_percent or 0.0,
                    min(item.before.get("count", 0),
                        item.after.get("count", 0))))
        if not out:
            out.append(
                "No metric changed by more than %.1f%% across %d/%d runs; "
                "no performance claim is supported." % (
                    self.tolerance_percent, self.before.runs, self.after.runs))
        return out

    def summary(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "ok": self.ok,
            "tolerance_percent": self.tolerance_percent,
            "min_runs": self.min_runs,
            "before": {"name": self.before.name, "runs": self.before.runs,
                       "succeeded": self.before.succeeded},
            "after": {"name": self.after.name, "runs": self.after.runs,
                      "succeeded": self.after.succeeded},
            "metrics": [item.to_dict() for item in self.metrics],
            "claims": self.claims(),
            "regressions": [item.metric for item in self.regressions],
            "improvements": [item.metric for item in self.improvements],
        }


def _fmt(value: Any) -> str:
    return "?" if value is None else ("%g" % float(value))


def compare(before: BenchmarkResult, after: BenchmarkResult, *,
            tolerance_percent: float = 2.0, min_runs: int = 3,
            metrics: Sequence[str] = (),
            higher_is_better: Sequence[str] = ()) -> Comparison:
    """Compare two benchmark results metric by metric.

    Built-in metrics carry their own direction. A caller-supplied metric
    (throughput, frames per second, requests per second) is assumed to be
    *lower is better* unless it is listed in ``higher_is_better`` — guessing
    the direction from a metric's name would turn a regression into a
    reported improvement.
    """
    wanted = list(metrics) if metrics else list(METRICS)
    up_is_good = {name for name in higher_is_better}
    comparison = Comparison(before=before, after=after,
                            tolerance_percent=float(tolerance_percent),
                            min_runs=int(min_runs))
    for metric in wanted:
        if metric in METRICS:
            unit, lower_is_better = METRICS[metric][1], METRICS[metric][2]
        else:
            unit, lower_is_better = "", metric not in up_is_good
        before_values = before.values(metric)
        after_values = after.values(metric)
        before_stats = statistics(before_values)
        after_stats = statistics(after_values)
        if not before_values or not after_values:
            comparison.metrics.append(MetricComparison(
                metric=metric, unit=unit, lower_is_better=lower_is_better,
                before=before_stats, after=after_stats, delta=None,
                delta_percent=None, verdict=NOT_MEASURED,
                reason="metric was not measured on both sides"))
            continue
        if (before_stats["count"] < min_runs
                or after_stats["count"] < min_runs):
            comparison.metrics.append(MetricComparison(
                metric=metric, unit=unit, lower_is_better=lower_is_better,
                before=before_stats, after=after_stats, delta=None,
                delta_percent=None, verdict=INSUFFICIENT_DATA,
                reason="%d/%d runs is below the %d-run minimum" % (
                    before_stats["count"], after_stats["count"], min_runs)))
            continue
        base = float(before_stats["median"])
        new = float(after_stats["median"])
        delta = new - base
        percent = (delta / base * 100.0) if base else (
            0.0 if delta == 0 else float("inf"))
        if percent == float("inf") or percent == float("-inf"):
            verdict = (REGRESSION if lower_is_better else IMPROVED)
            reason = "baseline was zero"
        elif abs(percent) < tolerance_percent:
            verdict = UNCHANGED
            reason = "within the %.1f%% tolerance" % tolerance_percent
        else:
            better = (new < base) if lower_is_better else (new > base)
            verdict = IMPROVED if better else REGRESSION
            reason = "median changed by %+.1f%% beyond the %.1f%% tolerance" % (
                percent, tolerance_percent)
        comparison.metrics.append(MetricComparison(
            metric=metric, unit=unit, lower_is_better=lower_is_better,
            before=before_stats, after=after_stats, delta=delta,
            delta_percent=percent, verdict=verdict, reason=reason))
    return comparison


class PerformanceLab:
    """Benchmarks commands and compares the results."""

    def __init__(self, root: str | Path = ".", *, runs: int = 3,
                 meter: Optional[ResourceMeter] = None) -> None:
        self.root = Path(root).resolve()
        self.runs = max(1, int(runs))
        self.meter = meter or ResourceMeter(self.root)
        self.history: List[BenchmarkResult] = []

    def benchmark(self, argv: Sequence[str], *, name: str = "",
                  runs: Optional[int] = None, timeout: Optional[float] = None,
                  cwd: str = "", label: str = "",
                  warmup: int = 0) -> BenchmarkResult:
        """Measure *argv* repeatedly and return the structured result."""
        count = self.runs if runs is None else max(1, int(runs))
        samples = self.meter.measure_repeated(
            argv, runs=count, timeout=timeout, cwd=cwd, warmup=warmup)
        result = BenchmarkResult(
            name=name or " ".join(argv)[:120], samples=samples,
            argv=tuple(argv), label=label)
        self.history.append(result)
        return result

    def compare_with(self, before: BenchmarkResult, argv: Sequence[str], *,
                     runs: Optional[int] = None, timeout: Optional[float] = None,
                     cwd: str = "", label: str = "after",
                     tolerance_percent: float = 2.0,
                     min_runs: Optional[int] = None) -> Comparison:
        """Benchmark *argv* as the "after" side and compare it to *before*."""
        after = self.benchmark(argv, runs=runs, timeout=timeout, cwd=cwd,
                               label=label)
        return compare(before, after, tolerance_percent=tolerance_percent,
                       min_runs=min_runs if min_runs is not None
                       else self.runs)

    def summary(self) -> Dict[str, Any]:
        return {
            "runs_per_benchmark": self.runs,
            "benchmarks": [result.summary() for result in self.history],
        }
