"""Evidence collection for self-analysis (A81).

Evidence is *observed*, never invented: every item names its source and
carries the raw measurement it was derived from. Collectors accept the
live objects Forge already maintains (failure ledger, run store, model
fabric, metrics registry, agent run logs, self-development history) and
degrade honestly when a source is absent — a missing source yields no
evidence, not synthetic evidence.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

MAX_ITEMS_PER_KIND = 200
MAX_DETAIL_CHARS = 400


class EvidenceKind(str, Enum):
    FAILURE = "failure"
    TEST_FAILURE = "test_failure"
    LATENCY = "latency"
    MODEL_PERFORMANCE = "model_performance"
    ROUTING_MISTAKE = "routing_mistake"
    REPEATED_ERROR = "repeated_error"
    AGENT_FAILURE = "agent_failure"
    RESOURCE_BOTTLENECK = "resource_bottleneck"


@dataclass
class Evidence:
    """One observed fact with its provenance."""

    id: str
    kind: str
    source: str
    summary: str
    measurement: dict[str, Any] = field(default_factory=dict)
    observed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Evidence":
        return cls(
            id=str(data["id"]), kind=str(data["kind"]),
            source=str(data.get("source", "")),
            summary=str(data.get("summary", "")),
            measurement=dict(data.get("measurement", {}) or {}),
            observed_at=float(data.get("observed_at", 0.0) or 0.0),
        )


def _clip(text: Any) -> str:
    return " ".join(str(text or "").split())[:MAX_DETAIL_CHARS]


_SUMMARY_RE = re.compile(
    r"(?:(\d+) passed)?(?:,\s*)?(?:(\d+) failed)?(?:,\s*)?(?:(\d+) error)?")
_FAILED_LINE_RE = re.compile(r"^(?:FAILED|ERROR)\s+([^\s]+)", re.M)


_SUMMARY_LINE_RE = re.compile(
    r"^(?:=+\s*)?((?:\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed|"
    r"deselected|warnings?|rerun)(?:,\s*)?)+)\s*in\s+[\d.]+s", re.M)


def parse_pytest_output(output: str) -> dict[str, Any]:
    """Extract counts and failing node ids from ``pytest`` output.

    Counts are taken from pytest's final summary line only (never from
    test names or captured output). The last summary line wins so wrapped
    or nested runs cannot inflate the numbers.
    """
    passed = failed = errors = skipped = 0
    summary_found = False
    for match in _SUMMARY_LINE_RE.finditer(output or ""):
        summary_found = True
        passed = failed = errors = skipped = 0
        for count_text, word in re.findall(r"(\d+)\s+(passed|failed|errors?|skipped)",
                                           match.group(1)):
            count = int(count_text)
            if word == "passed":
                passed = count
            elif word == "failed":
                failed = count
            elif word == "skipped":
                skipped = count
            else:
                errors = count
    failing = []
    seen: set[str] = set()
    for node in _FAILED_LINE_RE.findall(output or ""):
        if node not in seen:
            seen.add(node)
            failing.append(node)
    return {"passed": passed, "failed": failed, "errors": errors, "skipped": skipped,
            "summary_found": summary_found,
            "failing_tests": failing[:MAX_ITEMS_PER_KIND]}


class EvidenceCollector:
    """Gather evidence from Forge's own runtime records."""

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()
        self._items: list[Evidence] = []
        self._counter = 0

    # -- helpers ------------------------------------------------------------

    def _add(self, kind: EvidenceKind, source: str, summary: str,
             measurement: dict[str, Any] | None = None) -> Evidence:
        if sum(1 for e in self._items if e.kind == kind.value) >= MAX_ITEMS_PER_KIND:
            return self._items[-1]
        self._counter += 1
        item = Evidence(
            id=f"EV-{self._counter:04d}", kind=kind.value, source=source,
            summary=_clip(summary), measurement=dict(measurement or {}))
        self._items.append(item)
        return item

    def items(self) -> list[Evidence]:
        return list(self._items)

    # -- sources ------------------------------------------------------------

    def from_test_output(self, output: str, *, command: str = "pytest",
                         duration_seconds: float | None = None) -> None:
        """Record failing tests (and slow suites) from a real pytest run."""
        parsed = parse_pytest_output(output or "")
        for node in parsed["failing_tests"]:
            self._add(EvidenceKind.TEST_FAILURE, f"tests:{command}",
                      f"failing test {node}",
                      {"test": node, "passed": parsed["passed"],
                       "failed": parsed["failed"], "errors": parsed["errors"]})
        if parsed["failed"] or parsed["errors"]:
            if not parsed["failing_tests"]:
                self._add(EvidenceKind.TEST_FAILURE, f"tests:{command}",
                          f"{parsed['failed']} failed / {parsed['errors']} errors",
                          parsed)
        if duration_seconds is not None:
            self._add(EvidenceKind.LATENCY, f"tests:{command}",
                      f"test suite took {duration_seconds:.1f}s",
                      {"duration_seconds": float(duration_seconds),
                       "passed": parsed["passed"]})

    def from_failure_text(self, source: str, text: str) -> None:
        """A single observed failure (e.g. the test command not running)."""
        self._add(EvidenceKind.FAILURE, source, _clip(text), {"error": _clip(text)})

    def from_failure_ledger(self, ledger: Any, *, limit: int = 20) -> None:
        """A59 failure ledger: recurring fingerprints become evidence."""
        if ledger is None:
            return
        try:
            rows = ledger.top(limit)
        except Exception:
            return
        for row in rows:
            count = int(row.get("count", 0) or 0)
            category = str(row.get("category", ""))
            error = _clip(row.get("error", ""))
            kind = EvidenceKind.REPEATED_ERROR if count > 1 else EvidenceKind.FAILURE
            if category == "agent":
                kind = EvidenceKind.AGENT_FAILURE
            self._add(kind, "failure_ledger",
                      f"{category} failure x{count}: {error}",
                      {"category": category, "count": count,
                       "fingerprint": row.get("fingerprint", ""),
                       "error": error,
                       "first_seen": row.get("first_seen"),
                       "last_seen": row.get("last_seen")})

    def from_runs(self, runs: Iterable[Any]) -> None:
        """A34 run records: failed runs, rollbacks, slow executions."""
        rows = list(runs)
        durations: list[float] = []
        queue_waits: list[float] = []
        queued_now = 0
        for run in rows:
            status = str(getattr(run, "status", ""))
            status = status.split(".")[-1]
            if status == "QUEUED":
                queued_now += 1
            created = getattr(run, "created_at", None)
            started_at = getattr(run, "started_at", None)
            if created and started_at and started_at >= created:
                queue_waits.append(float(started_at - created))
            error = _clip(getattr(run, "error", ""))
            if status in ("FAILED", "ROLLED_BACK"):
                self._add(EvidenceKind.FAILURE, "run_store",
                          f"run {getattr(run, 'id', '?')} {status}: {error or 'no error text'}",
                          {"run_id": getattr(run, "id", ""), "status": status,
                           "error": error,
                           "model": getattr(run, "model", ""),
                           "provider": getattr(run, "provider", ""),
                           "attempts": getattr(run, "attempts", 1)})
            started = getattr(run, "started_at", None)
            finished = getattr(run, "finished_at", None)
            if started and finished and finished >= started:
                durations.append(float(finished - started))
            report = {}
            try:
                report = run.report() if callable(getattr(run, "report", None)) else {}
            except Exception:
                report = {}
            retries = int(report.get("retries", 0) or 0) if isinstance(report, dict) else 0
            if retries >= 2:
                self._add(EvidenceKind.FAILURE, "run_report",
                          f"run {getattr(run, 'id', '?')} needed {retries} repair retries",
                          {"run_id": getattr(run, "id", ""), "retries": retries})
        if durations:
            ordered = sorted(durations)
            p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
            mean = sum(ordered) / len(ordered)
            self._add(EvidenceKind.LATENCY, "run_store",
                      f"{len(ordered)} runs: mean {mean:.1f}s p95 {p95:.1f}s",
                      {"count": len(ordered), "mean_seconds": round(mean, 3),
                       "p95_seconds": round(p95, 3),
                       "max_seconds": round(ordered[-1], 3)})
        if queue_waits:
            ordered = sorted(queue_waits)
            p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
            if p95 >= 30.0:
                self._add(EvidenceKind.RESOURCE_BOTTLENECK, "run_store",
                          f"runs waited p95 {p95:.0f}s in the queue before starting",
                          {"count": len(ordered), "p95_queue_seconds": round(p95, 3)})
        if queued_now >= 5:
            self._add(EvidenceKind.RESOURCE_BOTTLENECK, "run_store",
                      f"{queued_now} runs currently queued and not started",
                      {"queued_runs": queued_now})

    def from_router_history(self, history: Iterable[dict[str, Any]], *,
                            route_events: Iterable[dict[str, Any]] | None = None,
                            latency_unit: str = "ms") -> None:
        """Model fabric feedback: per-model success/latency, plus fallbacks.

        ``history`` is the fabric router's feedback list (latency in **ms**;
        pass ``latency_unit="s"`` for the legacy ``ModelRouter`` whose
        history stores seconds). ``route_events`` are the fabric telemetry
        ``route`` events — the only place fallback decisions are recorded.
        """
        per_model: dict[str, dict[str, Any]] = {}
        scale = 1000.0 if latency_unit == "s" else 1.0
        for event in list(history or []):
            if not isinstance(event, dict):
                continue
            name = str(event.get("model", "") or "unknown")
            stats = per_model.setdefault(
                name, {"calls": 0, "failures": 0, "latency_total": 0.0,
                       "latency_count": 0, "errors": []})
            stats["calls"] += 1
            if event.get("failure") or event.get("success") is False:
                stats["failures"] += 1
                if event.get("error"):
                    stats["errors"].append(_clip(event.get("error")))
            latency = event.get("latency", event.get("latency_ms"))
            if isinstance(latency, (int, float)) and latency > 0:
                stats["latency_total"] += float(latency) * scale
                stats["latency_count"] += 1
        fallbacks: dict[str, dict[str, Any]] = {}
        for event in list(route_events or []):
            if not isinstance(event, dict) or not (event.get("fallback") or event.get("error")):
                continue
            reason = _clip(event.get("fallback_reason") or event.get("error") or "fallback")
            key = f"{event.get('capability', '')}|{reason}"
            entry = fallbacks.setdefault(key, {"count": 0, "models": set(),
                                               "capability": event.get("capability", ""),
                                               "reason": reason})
            entry["count"] += 1
            if event.get("model"):
                entry["models"].add(str(event["model"]))
        for entry in fallbacks.values():
            self._add(EvidenceKind.ROUTING_MISTAKE, "model_router",
                      f"routing fell back x{entry['count']} for capability "
                      f"{entry['capability'] or '?'}: {entry['reason']}",
                      {"capability": entry["capability"], "count": entry["count"],
                       "fallback_reason": entry["reason"],
                       "models": sorted(entry["models"])})
        for name, stats in sorted(per_model.items()):
            calls = stats["calls"]
            failures = stats["failures"]
            failure_rate = failures / calls if calls else 0.0
            mean_latency = (stats["latency_total"] / stats["latency_count"]
                            if stats["latency_count"] else 0.0)
            self._add(EvidenceKind.MODEL_PERFORMANCE, "model_router",
                      f"model {name}: {calls} calls, {failures} failures "
                      f"({failure_rate:.0%}), mean latency {mean_latency:.0f}ms",
                      {"model": name, "calls": calls, "failures": failures,
                       "failure_rate": round(failure_rate, 4),
                       "mean_latency_ms": round(mean_latency, 1),
                       "errors": stats["errors"][:5]})
            if calls >= 2 and failure_rate >= 0.5:
                self._add(EvidenceKind.ROUTING_MISTAKE, "model_router",
                          f"model {name} keeps being routed despite "
                          f"{failure_rate:.0%} failure rate",
                          {"model": name, "calls": calls,
                           "failure_rate": round(failure_rate, 4)})

    def from_agent_runs(self, runs: Iterable[dict[str, Any]]) -> None:
        """A51 agent run log entries (``success``/``error``/``elapsed_ms``)."""
        for entry in list(runs or []):
            if not isinstance(entry, dict):
                continue
            if entry.get("success", True):
                continue
            self._add(EvidenceKind.AGENT_FAILURE, "agent_runs",
                      f"agent {entry.get('agent', '?')} ({entry.get('role', '')}) "
                      f"failed: {_clip(entry.get('error', ''))}",
                      {"agent": entry.get("agent", ""), "role": entry.get("role", ""),
                       "error": _clip(entry.get("error", "")),
                       "elapsed_ms": entry.get("elapsed_ms")})

    def from_metrics(self, snapshot: dict[str, Any] | None, *,
                     slow_seconds: float = 120.0) -> None:
        """A62 metrics snapshot: slow phases, failure ratios, saturation.

        The plane's ``MetricsRegistry.observe`` takes **seconds** and its
        snapshot reports ``*_ms`` fields computed from them, so thresholds
        here are expressed in seconds and converted once.
        """
        if not isinstance(snapshot, dict):
            return
        slow_ms = slow_seconds * 1000.0
        for name, stats in (snapshot.get("latencies") or {}).items():
            if not isinstance(stats, dict):
                continue
            p95 = float(stats.get("p95_ms", 0.0) or 0.0)
            count = int(stats.get("count", 0) or 0)
            if count >= 3 and p95 >= slow_ms:
                self._add(EvidenceKind.LATENCY, "metrics",
                          f"{name} p95 {p95 / 1000.0:.1f}s over {count} samples",
                          {"metric": name, **stats})
        counters = snapshot.get("counters") or {}
        for prefix, label in (("runs", "task runs"), ("agent_runs", "agent runs")):
            ok = int(counters.get(f"{prefix}.succeeded", 0) or 0)
            bad = int(counters.get(f"{prefix}.failed", 0) or 0)
            total = ok + bad
            if total >= 3 and bad / total >= 0.5:
                kind = (EvidenceKind.AGENT_FAILURE if prefix == "agent_runs"
                        else EvidenceKind.REPEATED_ERROR)
                self._add(kind, "metrics",
                          f"{label}: {bad}/{total} failed ({bad / total:.0%}) this plane lifetime",
                          {"succeeded": ok, "failed": bad, "failure_rate": round(bad / total, 4)})
        gauges = snapshot.get("gauges") or {}
        active = int(gauges.get("active_runs", 0) or 0)
        workers = int(gauges.get("max_workers", 0) or 0)
        if workers and active >= workers:
            self._add(EvidenceKind.RESOURCE_BOTTLENECK, "metrics",
                      f"worker pool saturated: {active} active runs / {workers} workers",
                      {"active_runs": active, "max_workers": workers})
        queued = int(gauges.get("queued_runs", 0) or 0)
        if queued >= 5:
            self._add(EvidenceKind.RESOURCE_BOTTLENECK, "metrics",
                      f"queue backlog: {queued} runs queued and not started",
                      {"queued_runs": queued, "active_runs": active, "max_workers": workers})

    def from_self_history(self, history_dir: Path | None = None) -> None:
        """Earlier A26-A30 self-development runs: repeated rejections."""
        directory = history_dir or (self.root / ".forge" / "self" / "history")
        if not directory.is_dir():
            return
        reasons: dict[str, int] = {}
        for file in sorted(directory.glob("run_*.json"))[-100:]:
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("accepted"):
                continue
            reason = _clip(data.get("rejection_reason", "") or "rejected")
            reasons[reason] = reasons.get(reason, 0) + 1
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            kind = EvidenceKind.REPEATED_ERROR if count > 1 else EvidenceKind.FAILURE
            self._add(kind, "self_history",
                      f"self-development rejected x{count}: {reason}",
                      {"reason": reason, "count": count})

    def from_resources(self, *, disk_free_bytes: int | None = None,
                       min_free_bytes: int = 512 * 1024 * 1024,
                       thread_count: int | None = None,
                       max_threads: int = 200) -> None:
        """Host resource measurements supplied by the caller."""
        if disk_free_bytes is not None and disk_free_bytes < min_free_bytes:
            self._add(EvidenceKind.RESOURCE_BOTTLENECK, "host",
                      f"low disk: {disk_free_bytes // (1024 * 1024)} MiB free",
                      {"disk_free_bytes": int(disk_free_bytes),
                       "min_free_bytes": int(min_free_bytes)})
        if thread_count is not None and thread_count > max_threads:
            self._add(EvidenceKind.RESOURCE_BOTTLENECK, "host",
                      f"{thread_count} live threads (limit {max_threads})",
                      {"thread_count": int(thread_count),
                       "max_threads": int(max_threads)})

    def measure_host(self) -> None:
        """Measure disk/thread headroom on this host (best effort)."""
        import shutil
        import threading

        try:
            usage = shutil.disk_usage(str(self.root))
            free = int(usage.free)
        except OSError:
            free = None
        self.from_resources(disk_free_bytes=free,
                            thread_count=threading.active_count())
