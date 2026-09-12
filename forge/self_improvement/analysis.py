"""Self-analysis (A81): from observed evidence to ranked weaknesses.

The analyzer never runs the test-suite itself unless asked (``run_tests``)
and never invents measurements. Each weakness carries the ids of the
evidence it was derived from, a metric with the measured value and a
target, and the files it most plausibly concerns (resolved through the
repository's own intelligence where possible).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from forge.self_improvement.evidence import Evidence, EvidenceCollector, EvidenceKind
from forge.self_improvement.guardrails import is_protected_path

SEVERITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1}
MAX_WEAKNESSES = 25


@dataclass
class Weakness:
    id: str
    category: str
    severity: str
    title: str
    metric: str
    value: Any
    target: Any
    evidence_ids: list[str]
    affected_files: list[str] = field(default_factory=list)
    suggested_action: str = ""
    risk_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Weakness":
        return cls(
            id=str(data["id"]), category=str(data.get("category", "")),
            severity=str(data.get("severity", "low")),
            title=str(data.get("title", "")), metric=str(data.get("metric", "")),
            value=data.get("value"), target=data.get("target"),
            evidence_ids=[str(x) for x in data.get("evidence_ids", [])],
            affected_files=[str(x) for x in data.get("affected_files", [])],
            suggested_action=str(data.get("suggested_action", "")),
            risk_notes=str(data.get("risk_notes", "")),
        )


@dataclass
class SelfAnalysisReport:
    root: str
    generated_at: float
    evidence: list[Evidence]
    weaknesses: list[Weakness]
    metrics: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root, "generated_at": self.generated_at,
            "evidence": [e.to_dict() for e in self.evidence],
            "weaknesses": [w.to_dict() for w in self.weaknesses],
            "metrics": dict(self.metrics), "sources": list(self.sources),
            "summary": self.summary(),
        }

    def summary(self) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        for item in self.evidence:
            by_kind[item.kind] = by_kind.get(item.kind, 0) + 1
        by_severity: dict[str, int] = {}
        for item in self.weaknesses:
            by_severity[item.severity] = by_severity.get(item.severity, 0) + 1
        return {"evidence_total": len(self.evidence), "evidence_by_kind": by_kind,
                "weaknesses_total": len(self.weaknesses),
                "weaknesses_by_severity": by_severity}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SelfAnalysisReport":
        return cls(
            root=str(data.get("root", "")),
            generated_at=float(data.get("generated_at", 0.0) or 0.0),
            evidence=[Evidence.from_dict(e) for e in data.get("evidence", [])],
            weaknesses=[Weakness.from_dict(w) for w in data.get("weaknesses", [])],
            metrics=dict(data.get("metrics", {}) or {}),
            sources=list(data.get("sources", []) or []),
        )


_TEST_NODE_RE = re.compile(r"^([^:]+\.py)(?:::.*)?$")


class SelfAnalyzer:
    """Build a :class:`SelfAnalysisReport` for the Forge checkout at ``root``."""

    def __init__(self, root: str | Path = ".", *, output_dir: Path | None = None) -> None:
        self.root = Path(root).resolve()
        self.output_dir = output_dir or (self.root / ".forge" / "self_improvement")

    # -- evidence gathering ---------------------------------------------------

    def collect(self, *, failure_ledger: Any = None, runs: Iterable[Any] | None = None,
                router_history: Iterable[dict[str, Any]] | None = None,
                agent_runs: Iterable[dict[str, Any]] | None = None,
                metrics_snapshot: dict[str, Any] | None = None,
                test_output: str | None = None, test_duration: float | None = None,
                run_tests: bool = False, test_timeout: int = 900,
                measure_host: bool = True) -> tuple[EvidenceCollector, list[str]]:
        collector = EvidenceCollector(self.root)
        sources: list[str] = []
        if run_tests and test_output is None:
            test_output, test_duration = self._run_tests(test_timeout)
        if test_output is not None:
            collector.from_test_output(test_output, duration_seconds=test_duration)
            sources.append("tests")
        if failure_ledger is not None:
            collector.from_failure_ledger(failure_ledger)
            sources.append("failure_ledger")
        if runs is not None:
            collector.from_runs(runs)
            sources.append("run_store")
        if router_history is not None:
            collector.from_router_history(router_history)
            sources.append("model_router")
        if agent_runs is not None:
            collector.from_agent_runs(agent_runs)
            sources.append("agent_runs")
        if metrics_snapshot is not None:
            collector.from_metrics(metrics_snapshot)
            sources.append("metrics")
        collector.from_self_history()
        sources.append("self_history")
        if measure_host:
            collector.measure_host()
            sources.append("host")
        return collector, sources

    def _run_tests(self, timeout: int) -> tuple[str, float]:
        started = time.perf_counter()
        command = [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                   "-x", "--no-header", "-rfE"]
        try:
            process = subprocess.run(command, cwd=self.root, text=True,
                                     capture_output=True, timeout=timeout, check=False)
            output = process.stdout + process.stderr
        except (OSError, subprocess.TimeoutExpired) as exc:
            output = f"FAILED test-run: {exc}"
        return output, time.perf_counter() - started

    # -- analysis -------------------------------------------------------------

    def analyze(self, **collect_kwargs: Any) -> SelfAnalysisReport:
        collector, sources = self.collect(**collect_kwargs)
        evidence = collector.items()
        weaknesses = self.derive_weaknesses(evidence)
        report = SelfAnalysisReport(
            root=str(self.root), generated_at=time.time(), evidence=evidence,
            weaknesses=weaknesses, metrics=self._metrics(evidence), sources=sources)
        self._persist(report)
        return report

    def _persist(self, report: SelfAnalysisReport) -> None:
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            (self.output_dir / "analysis.json").write_text(
                json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
        except OSError:
            pass

    def _metrics(self, evidence: list[Evidence]) -> dict[str, Any]:
        metrics: dict[str, Any] = {}
        tests = [e for e in evidence if e.kind == EvidenceKind.TEST_FAILURE.value]
        metrics["failing_tests"] = len({e.measurement.get("test") for e in tests if e.measurement.get("test")})
        for item in evidence:
            if item.kind == EvidenceKind.LATENCY.value and item.source.startswith("tests"):
                metrics["test_suite_seconds"] = round(float(item.measurement.get("duration_seconds") or 0.0), 2)
            if item.kind == EvidenceKind.LATENCY.value and item.source == "run_store":
                metrics["run_p95_seconds"] = item.measurement.get("p95_seconds")
        models = [e for e in evidence if e.kind == EvidenceKind.MODEL_PERFORMANCE.value]
        if models:
            metrics["model_failure_rate_max"] = max(
                float(e.measurement.get("failure_rate", 0.0)) for e in models)
        metrics["repeated_errors"] = sum(
            1 for e in evidence if e.kind == EvidenceKind.REPEATED_ERROR.value)
        metrics["agent_failures"] = sum(
            1 for e in evidence if e.kind == EvidenceKind.AGENT_FAILURE.value)
        metrics["routing_mistakes"] = sum(
            1 for e in evidence if e.kind == EvidenceKind.ROUTING_MISTAKE.value)
        metrics["resource_bottlenecks"] = sum(
            1 for e in evidence if e.kind == EvidenceKind.RESOURCE_BOTTLENECK.value)
        return metrics

    # -- weakness derivation --------------------------------------------------

    def derive_weaknesses(self, evidence: list[Evidence]) -> list[Weakness]:
        weaknesses: list[Weakness] = []
        counter = 0

        def new_id() -> str:
            nonlocal counter
            counter += 1
            return f"WEAK-{counter:03d}"

        # 1. Failing tests -> one weakness per test file.
        by_file: dict[str, list[Evidence]] = {}
        for item in evidence:
            if item.kind != EvidenceKind.TEST_FAILURE.value:
                continue
            node = str(item.measurement.get("test", ""))
            match = _TEST_NODE_RE.match(node)
            key = match.group(1) if match else "tests"
            by_file.setdefault(key, []).append(item)
        for test_file, items in sorted(by_file.items()):
            sources = self._sources_for_test(test_file)
            weaknesses.append(Weakness(
                id=new_id(), category="test_failure", severity="high",
                title=f"{len(items)} failing test(s) in {test_file}",
                metric="failing_tests", value=len(items), target=0,
                evidence_ids=[e.id for e in items],
                affected_files=[p for p in sources if not is_protected_path(p)] or
                ([test_file] if not is_protected_path(test_file) else []),
                suggested_action=f"Fix the root cause behind failing tests in {test_file}",
                risk_notes="behavioral change; must keep every other test green"))

        # 2. Repeated errors (failure ledger / self history).
        for item in evidence:
            if item.kind != EvidenceKind.REPEATED_ERROR.value:
                continue
            count = int(item.measurement.get("count", 0) or 0)
            files = self._files_from_text(item.measurement.get("error", "") or item.summary)
            weaknesses.append(Weakness(
                id=new_id(), category="repeated_error",
                severity="high" if count >= 5 else "medium",
                title=f"repeated error ({count}x): {item.summary[:100]}",
                metric="error_recurrence", value=count, target=0,
                evidence_ids=[item.id], affected_files=files,
                suggested_action=f"Eliminate the recurring failure: {item.measurement.get('error', item.summary)[:160]}",
                risk_notes="root cause may span modules; keep the change minimal"))

        # 3. Agent failures.
        agent_items = [e for e in evidence if e.kind == EvidenceKind.AGENT_FAILURE.value]
        by_agent: dict[str, list[Evidence]] = {}
        for item in agent_items:
            by_agent.setdefault(str(item.measurement.get("agent") or item.measurement.get("category") or "agent"), []).append(item)
        for agent, items in sorted(by_agent.items()):
            files = sorted({f for e in items for f in self._files_from_text(e.measurement.get("error", ""))})
            weaknesses.append(Weakness(
                id=new_id(), category="agent_failure",
                severity="medium" if len(items) < 3 else "high",
                title=f"agent {agent} failed {len(items)} time(s)",
                metric="agent_failures", value=len(items), target=0,
                evidence_ids=[e.id for e in items],
                affected_files=files or ["forge/agents/runner.py"],
                suggested_action=f"Harden agent {agent} against: {items[0].measurement.get('error', '')[:120]}",
                risk_notes="agent behavior change; cannot widen agent capabilities"))

        # 4. Model performance / routing.
        for item in evidence:
            if item.kind == EvidenceKind.ROUTING_MISTAKE.value:
                weaknesses.append(Weakness(
                    id=new_id(), category="routing_mistake", severity="medium",
                    title=item.summary[:120], metric="routing_mistakes", value=1, target=0,
                    evidence_ids=[item.id],
                    affected_files=["forge/models/router.py"],
                    suggested_action=("Adjust routing scores so unhealthy models are "
                                      "deprioritized sooner: " + item.summary[:100]),
                    risk_notes="routing changes affect every model call; must stay deterministic"))
            elif item.kind == EvidenceKind.MODEL_PERFORMANCE.value:
                rate = float(item.measurement.get("failure_rate", 0.0) or 0.0)
                calls = int(item.measurement.get("calls", 0) or 0)
                if calls >= 3 and rate >= 0.3:
                    weaknesses.append(Weakness(
                        id=new_id(), category="model_performance",
                        severity="high" if rate >= 0.6 else "medium",
                        title=f"model {item.measurement.get('model')} failure rate {rate:.0%}",
                        metric="model_failure_rate", value=round(rate, 3), target=0.1,
                        evidence_ids=[item.id],
                        affected_files=["forge/models/router.py", "forge/models/fabric.py"],
                        suggested_action=(f"Lower reliability weight for model "
                                          f"{item.measurement.get('model')} or improve its prompt handling"),
                        risk_notes="must not disable providers or bypass data policy"))

        # 5. Latency.
        for item in evidence:
            if item.kind != EvidenceKind.LATENCY.value:
                continue
            m = item.measurement
            if item.source.startswith("tests") and float(m.get("duration_seconds", 0) or 0) > 300:
                weaknesses.append(Weakness(
                    id=new_id(), category="latency", severity="low",
                    title=f"test suite is slow ({float(m['duration_seconds']):.0f}s)",
                    metric="test_suite_seconds", value=round(float(m["duration_seconds"]), 1),
                    target=round(float(m["duration_seconds"]) * 0.8, 1),
                    evidence_ids=[item.id], affected_files=["tests/conftest.py"],
                    suggested_action="Reduce slow fixtures/sleeps in the test-suite without weakening assertions",
                    risk_notes="must not skip or weaken tests"))
            elif item.source == "run_store" and float(m.get("p95_seconds", 0) or 0) > 120:
                weaknesses.append(Weakness(
                    id=new_id(), category="latency", severity="medium",
                    title=f"run p95 latency {float(m['p95_seconds']):.0f}s",
                    metric="run_p95_seconds", value=round(float(m["p95_seconds"]), 1),
                    target=round(float(m["p95_seconds"]) * 0.8, 1),
                    evidence_ids=[item.id],
                    affected_files=["forge/core/supervisor.py"],
                    suggested_action="Cut redundant verification/benchmark passes in the supervisor pipeline",
                    risk_notes="no gate may be skipped to save time"))
            elif item.source == "metrics":
                weaknesses.append(Weakness(
                    id=new_id(), category="latency", severity="medium",
                    title=item.summary[:120], metric=str(m.get("metric", "latency")),
                    value=m.get("p95_ms"), target=round(float(m.get("p95_ms", 0) or 0) * 0.8, 1),
                    evidence_ids=[item.id], affected_files=["forge/core/supervisor.py"],
                    suggested_action=f"Profile and reduce {m.get('metric', 'this phase')}",
                    risk_notes="no gate may be skipped to save time"))

        # 6. Resource bottlenecks.
        for item in evidence:
            if item.kind != EvidenceKind.RESOURCE_BOTTLENECK.value:
                continue
            weaknesses.append(Weakness(
                id=new_id(), category="resource_bottleneck", severity="medium",
                title=item.summary[:120], metric="resource_bottlenecks", value=1, target=0,
                evidence_ids=[item.id],
                affected_files=["forge/core/task_queue.py"] if "queue" in item.summary or "worker" in item.summary else [],
                suggested_action="Relieve the bottleneck: " + item.summary[:100],
                risk_notes="capacity changes must stay bounded"))

        # 7. Plain failures (single-occurrence) grouped by error text.
        singles: dict[str, list[Evidence]] = {}
        for item in evidence:
            if item.kind == EvidenceKind.FAILURE.value:
                key = str(item.measurement.get("error") or item.measurement.get("reason") or item.summary)[:80]
                singles.setdefault(key, []).append(item)
        for key, items in sorted(singles.items()):
            files = sorted({f for e in items for f in self._files_from_text(key)})
            weaknesses.append(Weakness(
                id=new_id(), category="failure", severity="low" if len(items) == 1 else "medium",
                title=f"failure: {key}", metric="failures", value=len(items), target=0,
                evidence_ids=[e.id for e in items], affected_files=files,
                suggested_action=f"Investigate and fix: {key}",
                risk_notes="single observation; low confidence"))

        weaknesses.sort(key=lambda w: (-SEVERITY_ORDER.get(w.severity, 0),
                                       -len(w.evidence_ids), w.id))
        return weaknesses[:MAX_WEAKNESSES]

    # -- file resolution helpers ----------------------------------------------

    def _sources_for_test(self, test_file: str) -> list[str]:
        """Map a test file to plausible source files via import names."""
        path = self.root / test_file
        candidates: list[str] = []
        if not path.is_file():
            return candidates
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return candidates
        for match in re.finditer(r"^from\s+(forge(?:\.[\w]+)+)\s+import", text, re.M):
            rel = match.group(1).replace(".", "/") + ".py"
            if (self.root / rel).is_file() and rel not in candidates:
                candidates.append(rel)
        return candidates[:6]

    def _files_from_text(self, text: str) -> list[str]:
        """Pull repository-relative python paths mentioned in an error."""
        found: list[str] = []
        for match in re.finditer(r"((?:forge|tests)/[\w/]+\.py)", str(text or "")):
            rel = match.group(1)
            if (self.root / rel).is_file() and rel not in found and not is_protected_path(rel):
                found.append(rel)
        for match in re.finditer(r"\b(forge(?:\.[\w]+)+)\b", str(text or "")):
            rel = match.group(1).replace(".", "/") + ".py"
            if (self.root / rel).is_file() and rel not in found and not is_protected_path(rel):
                found.append(rel)
        return found[:6]
