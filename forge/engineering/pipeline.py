"""Engineering pipeline (A83): run the team, safely.

The pipeline turns a roster into an execution order using the ``reads`` and
``writes`` each agent declared — not a hard-coded sequence:

* an agent runs after every agent that writes something it reads;
* agents whose declared reads/writes do not overlap may run in parallel,
  bounded by ``max_parallel``;
* **mutating agents never run in parallel.** Two agents editing the same tree
  is a data race, and Forge will not pretend otherwise. A mutating agent is
  serialised against everything, and the report says that happened;
* every run is recorded into the shared :class:`ProjectContext`, so the next
  pipeline run starts from what this one learned.

A stage that fails does not silently let dependents run: dependents are marked
``blocked`` with the reason.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from forge.engineering.context import ProjectContext
from forge.engineering.roster import AgentSpec, Roster, default_roster

PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
BLOCKED = "blocked"
SKIPPED = "skipped"
MAX_PARALLEL = 8


@dataclass
class StageResult:
    """What one agent did in one pipeline run."""

    role: str
    status: str
    output: str = ""
    error: str = ""
    duration_ms: float = 0.0
    #: Roles this stage had to wait for.
    waited_for: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"role": self.role, "status": self.status,
                "duration_ms": round(self.duration_ms, 1),
                "waited_for": list(self.waited_for),
                "error": self.error, "metadata": dict(self.metadata),
                "output": self.output[-4000:]}


@dataclass
class PipelineReport:
    """The outcome of a whole team run."""

    status: str
    stages: List[StageResult] = field(default_factory=list)
    duration_ms: float = 0.0
    #: Roles that ran concurrently, recorded so parallelism is auditable.
    parallel_groups: List[List[str]] = field(default_factory=list)
    context_revision: int = 0

    @property
    def ok(self) -> bool:
        return self.status == SUCCEEDED

    def stage(self, role: str) -> Optional[StageResult]:
        for item in self.stages:
            if item.role == role:
                return item
        return None

    def summary(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        for item in self.stages:
            counts[item.status] = counts.get(item.status, 0) + 1
        return {
            "status": self.status, "ok": self.ok,
            "stages": len(self.stages),
            "by_status": {key: counts[key] for key in sorted(counts)},
            "duration_ms": round(self.duration_ms, 1),
            "parallel_groups": [list(group)
                                for group in self.parallel_groups],
            "context_revision": self.context_revision,
            "detail": [item.to_dict() for item in self.stages],
        }

    def render(self) -> str:
        lines = ["pipeline: %s (%.0f ms)" % (self.status, self.duration_ms)]
        for item in self.stages:
            waited = " after %s" % ",".join(item.waited_for) \
                if item.waited_for else ""
            lines.append("  %-14s %-9s %6.0f ms%s%s" % (
                item.role, item.status, item.duration_ms, waited,
                " — %s" % item.error if item.error else ""))
        for group in self.parallel_groups:
            if len(group) > 1:
                lines.append("  parallel: %s" % ", ".join(group))
        return "\n".join(lines)


def order_specs(specs: Sequence[AgentSpec]) -> List[List[AgentSpec]]:
    """Group agents into waves that may run concurrently.

    Wave 0 is everything with no unsatisfied reads. Each later wave contains
    agents whose reads are satisfied by earlier waves. Mutating agents are
    always alone in their wave — they are never scheduled beside another
    agent, mutating or not.
    """
    remaining = list(specs)
    produced: set = set()
    waves: List[List[AgentSpec]] = []
    guard = 0
    while remaining and guard < 100:
        guard += 1
        ready = [spec for spec in remaining
                 if all(key in produced for key in spec.reads)]
        if not ready:
            # Unresolvable reads: run what is left in dependency-name order
            # rather than silently dropping work.
            waves.append(sorted(remaining, key=lambda item: item.role))
            break
        mutating = [spec for spec in ready if spec.mutating]
        if mutating:
            # Serialise: one mutating agent per wave, nothing beside it.
            chosen = sorted(mutating, key=lambda item: item.role)[0]
            wave = [chosen]
        else:
            wave = sorted(ready, key=lambda item: item.role)
        waves.append(wave)
        for spec in wave:
            produced.update(spec.writes)
            if spec in remaining:
                remaining.remove(spec)
    return waves


class EngineeringPipeline:
    """Runs the roster against a shared project context."""

    def __init__(self, root: str | Path = ".", *, profile: Any = None,
                 roster: Optional[Roster] = None,
                 context: Optional[ProjectContext] = None,
                 max_parallel: int = 4) -> None:
        self.root = Path(root).resolve()
        self.profile = profile
        self.roster = roster or default_roster()
        self.context = context or ProjectContext(
            self.root.name or "project", root=self.root)
        self.max_parallel = max(1, min(int(max_parallel), MAX_PARALLEL))

    def plan_waves(self, roles: Sequence[str] = ()) -> List[List[AgentSpec]]:
        specs = [spec for spec in self.roster.specs
                 if not roles or spec.role in set(roles)]
        return order_specs(specs)

    def run(self, roles: Sequence[str] = ()) -> PipelineReport:
        """Execute the selected agents, in dependency order."""
        started = time.monotonic()
        specs_by_role = {spec.role: spec for spec in self.roster.specs}
        waves = self.plan_waves(roles)
        report = PipelineReport(status=SUCCEEDED)
        failed: set = set()

        for wave in waves:
            group = [spec.role for spec in wave]
            report.parallel_groups.append(group)
            runnable: List[AgentSpec] = []
            for spec in wave:
                blocked_by = [key for key in spec.reads
                              if key in failed or self._missing_read(key)]
                if blocked_by:
                    report.stages.append(StageResult(
                        role=spec.role, status=BLOCKED,
                        error="missing inputs: %s" % ", ".join(
                            sorted(set(blocked_by))),
                        waited_for=sorted(set(blocked_by))))
                    report.status = FAILED
                    failed.add(spec.role)
                    continue
                runnable.append(spec)

            if len(runnable) > 1 and not any(
                    spec.mutating for spec in runnable):
                results = self._run_parallel(runnable)
            else:
                results = [self._run_one(spec) for spec in runnable]

            for spec, result in zip(runnable, results):
                report.stages.append(result)
                if result.status != SUCCEEDED:
                    report.status = FAILED
                    failed.add(spec.role)

        report.duration_ms = (time.monotonic() - started) * 1000
        report.context_revision = self.context.revision
        return report

    # -- execution -------------------------------------------------------

    def _missing_read(self, key: str) -> bool:
        """True when a declared input was never recorded by anyone."""
        if not key:
            return False
        # Inputs that the pipeline itself produces are checked against the
        # context; free-form inputs like ``requirement`` are the caller's job
        # and are not treated as missing.
        produced = {written for spec in self.roster.specs
                    for written in spec.writes}
        if key not in produced:
            return False
        return self.context.get(key) is None

    def _run_parallel(self, specs: Sequence[AgentSpec]
                      ) -> List[StageResult]:
        results: List[Optional[StageResult]] = [None] * len(specs)
        limit = threading.Semaphore(self.max_parallel)
        lock = threading.Lock()

        def worker(index: int, spec: AgentSpec) -> None:
            with limit:
                result = self._run_one(spec)
            with lock:
                results[index] = result

        threads = [threading.Thread(target=worker, args=(index, spec))
                   for index, spec in enumerate(specs)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return [result if result is not None else StageResult(
            role=spec.role, status=FAILED, error="worker returned nothing")
            for result, spec in zip(results, specs)]

    def _run_one(self, spec: AgentSpec) -> StageResult:
        started = time.monotonic()
        if spec.factory is None:
            return StageResult(
                role=spec.role, status=SKIPPED,
                duration_ms=(time.monotonic() - started) * 1000,
                error="no implementation registered for this role")
        try:
            agent = spec.factory(self.root, self.context, self.profile)
        except Exception as exc:  # noqa: BLE001 - recorded, never fatal
            return StageResult(
                role=spec.role, status=FAILED,
                duration_ms=(time.monotonic() - started) * 1000,
                error="could not construct the agent: %s" % exc)
        attach = getattr(agent, "attach", None)
        if callable(attach):
            attach(self.context)
        request = _make_request(spec, self.context)
        try:
            response = agent.execute(request)
        except Exception as exc:  # noqa: BLE001 - recorded, never fatal
            return StageResult(
                role=spec.role, status=FAILED,
                duration_ms=(time.monotonic() - started) * 1000,
                error="agent raised: %s" % exc)
        ok = bool(getattr(response, "success", False))
        return StageResult(
            role=spec.role, status=SUCCEEDED if ok else FAILED,
            output=str(getattr(response, "output", "") or ""),
            error=str(getattr(response, "error", "") or ""),
            duration_ms=(time.monotonic() - started) * 1000,
            metadata=dict(getattr(response, "metadata", {}) or {}))


def _make_request(spec: AgentSpec, context: ProjectContext) -> Any:
    """Build a minimal AgentRequest-shaped object for an engineering agent.

    The real :class:`~forge.agents.execution.AgentRequest` needs a
    :class:`~forge.core.task_engine.Task`; engineering agents only need the
    requirement text and per-role metadata, so the request is assembled from
    the shared context.
    """
    from forge.agents.execution import AgentRequest
    from forge.core.task_engine import Task, TaskStatus

    requirement = str(context.get("requirement", "") or "")
    task = Task(id="engineering-%s" % spec.role,
                description=requirement or spec.responsibility)
    metadata: Dict[str, str] = {}
    if requirement:
        metadata["requirement"] = requirement
    plan = context.get("plan")
    if isinstance(plan, dict):
        metadata["plan_id"] = str(plan.get("id", ""))
    return AgentRequest(task=task, stage=TaskStatus.RUNNING,
                        metadata=metadata)
