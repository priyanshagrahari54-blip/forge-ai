"""Engineering agent roster (A83): fourteen specialists, one shared context.

Each agent has exactly one job and one way of being judged. They are real
:class:`~forge.agents.execution.AgentExecutor` implementations, so they slot
into the registry, planner, and orchestrator Forge already has — this module
does not invent a parallel agent system.

What makes them a *team* rather than a list:

* every agent receives the same :class:`~forge.engineering.context.ProjectContext`
  and records what it learned into it, so the next agent starts from evidence
  instead of re-deriving it;
* each agent declares ``reads`` and ``writes`` — the fact keys it consumes and
  produces. The pipeline uses that to decide what can safely run in parallel
  and what must wait, instead of guessing;
* an agent that cannot do its job says so. Missing toolchain, no tests
  collected, no probe available: each is reported as its own status, never
  dressed up as success.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from forge.engineering.context import ProjectContext

#: Canonical engineering roles, in the order a project usually needs them.
ROLES: Tuple[str, ...] = (
    "architect", "research", "coding", "review", "debugging", "testing",
    "security", "performance", "build", "documentation", "release",
    "dependency", "hardware", "devops",
)


@dataclass
class AgentSpec:
    """Declarative description of one engineering agent."""

    role: str
    name: str
    responsibility: str
    #: Fact keys this agent needs to have been recorded before it runs.
    reads: Tuple[str, ...] = ()
    #: Fact keys this agent records when it runs.
    writes: Tuple[str, ...] = ()
    #: True when the agent mutates the repository.
    mutating: bool = False
    #: Executor factory: ``(root, context, profile) -> object with .execute``.
    factory: Optional[Callable[..., Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"role": self.role, "name": self.name,
                "responsibility": self.responsibility,
                "reads": list(self.reads), "writes": list(self.writes),
                "mutating": self.mutating,
                "implemented": self.factory is not None}


def _build_agent(root: Path, context: ProjectContext, profile: Any) -> Any:
    from forge.builder.engine import BuildAgent
    return BuildAgent(root, profile=profile)


def _test_agent(root: Path, context: ProjectContext, profile: Any) -> Any:
    return TestAgent(root, profile=profile)


def _debug_agent(root: Path, context: ProjectContext, profile: Any) -> Any:
    return DebugAgent(root, profile=profile)


def _perf_agent(root: Path, context: ProjectContext, profile: Any) -> Any:
    return PerfAgent(root)


def _hardware_agent(root: Path, context: ProjectContext, profile: Any) -> Any:
    from forge.hardware.agent import HardwareAgent
    return HardwareAgent(root, profile=profile)


def _vm_agent(root: Path, context: ProjectContext, profile: Any) -> Any:
    from forge.vm.qemu import BootTestAgent
    return BootTestAgent(root, profile=profile)


def _architect_agent(root: Path, context: ProjectContext, profile: Any) -> Any:
    return ArchitectAgent(root, profile=profile)


@dataclass
class Roster:
    """The engineering team, with each agent's contract made explicit."""

    specs: List[AgentSpec] = field(default_factory=list)

    def spec(self, role: str) -> Optional[AgentSpec]:
        for item in self.specs:
            if item.role == role:
                return item
        return None

    def roles(self) -> List[str]:
        return [item.role for item in self.specs]

    def implemented(self) -> List[str]:
        return [item.role for item in self.specs if item.factory is not None]

    def to_dict(self) -> Dict[str, Any]:
        return {"agents": [item.to_dict() for item in self.specs],
                "count": len(self.specs),
                "implemented": self.implemented()}


def default_roster() -> Roster:
    """The standard team. Every entry states what it reads and writes."""
    return Roster(specs=[
        AgentSpec(
            role="architect", name="architect",
            responsibility="Turns the requirement into specifications, "
                           "architecture, components, and an editable plan.",
            reads=("requirement",), writes=("plan", "architecture",
                                            "specifications"),
            factory=_architect_agent),
        AgentSpec(
            role="research", name="research",
            responsibility="Finds prior art, constraints, and known failure "
                           "modes for the chosen approach.",
            reads=("plan",), writes=("research",)),
        AgentSpec(
            role="dependency", name="dependency",
            responsibility="Resolves and records external dependencies and "
                           "their risk; never installs silently.",
            reads=("plan",), writes=("dependencies",)),
        AgentSpec(
            role="coding", name="coding",
            responsibility="Implements components against the plan.",
            reads=("plan", "architecture"), writes=("changes",),
            mutating=True),
        AgentSpec(
            role="build", name="build",
            responsibility="Builds with the project's own toolchain and "
                           "reports real compiler diagnostics.",
            reads=("changes",), writes=("build",),
            factory=_build_agent),
        AgentSpec(
            role="testing", name="testing",
            responsibility="Runs the staged test pipeline and reports "
                           "per-test outcomes.",
            reads=("build",), writes=("tests",),
            factory=_test_agent),
        AgentSpec(
            role="debugging", name="debugging",
            responsibility="Diagnoses a failure, proposes and verifies a "
                           "bounded fix, records every attempt.",
            reads=("tests", "build"), writes=("debug", "changes"),
            mutating=True, factory=_debug_agent),
        AgentSpec(
            role="review", name="review",
            responsibility="Reviews the implementation against the "
                           "specifications.",
            reads=("changes", "specifications"), writes=("review",)),
        AgentSpec(
            role="security", name="security",
            responsibility="Reviews the change for security impact and "
                           "records findings by severity.",
            reads=("changes",), writes=("security",)),
        AgentSpec(
            role="performance", name="performance",
            responsibility="Measures before/after and gates regressions; the "
                           "only agent allowed to claim an improvement.",
            reads=("tests",), writes=("benchmark",),
            factory=_perf_agent),
        AgentSpec(
            role="hardware", name="hardware",
            responsibility="Probes real hardware and reports SUPPORTED / "
                           "PARTIALLY_SUPPORTED / UNSUPPORTED / UNKNOWN with "
                           "the probe that produced each status.",
            reads=("plan",), writes=("hardware",),
            factory=_hardware_agent),
        AgentSpec(
            role="devops", name="devops",
            responsibility="Owns the VM/boot harness, CI wiring, and "
                           "environment prerequisites.",
            reads=("build",), writes=("boot", "environment"),
            factory=_vm_agent),
        AgentSpec(
            role="documentation", name="documentation",
            responsibility="Writes setup, usage, and limitations; states what "
                           "is not supported.",
            reads=("changes", "tests"), writes=("documentation",)),
        AgentSpec(
            role="release", name="release",
            responsibility="Packages and stages the artifact with a rollback "
                           "path.",
            reads=("build", "tests", "security"), writes=("release",)),
    ])


# =========================================================================
# agents implemented on top of the A83 engines
# =========================================================================


def _response(agent: str, request: Any, *, ok: bool, output: str,
              error: str = "", **metadata: Any) -> Any:
    from forge.agents.execution import AgentResponse
    return AgentResponse(
        success=ok, output=output, error=error, agent=agent,
        stage=request.stage,
        context_fingerprint=(request.context.fingerprint
                             if request.context else ""),
        metadata={key: str(value) for key, value in metadata.items()})


class ContextAgent:
    """Base for A83 agents: one job, one shared context, honest status."""

    __test__ = False
    name = "context-agent"
    role = "context-agent"

    def __init__(self, root: str | Path = ".", *, profile: Any = None,
                 context: Optional[ProjectContext] = None) -> None:
        self.root = Path(root).resolve()
        self.profile = profile
        self.context = context

    def describe(self) -> str:
        return "Engineering agent for %s." % self.role

    def attach(self, context: ProjectContext) -> "ContextAgent":
        self.context = context
        return self

    def record(self, key: str, kind: str, value: Any, *,
               note: str = "", confidence: float = 1.0) -> None:
        if self.context is None:
            return
        self.context.record(key, kind, value, source="agent:%s" % self.name,
                            note=note, confidence=confidence)

    def note(self, summary: str, *, ok: bool = True, **detail: Any) -> None:
        if self.context is None:
            return
        self.context.note_turn(self.name, self.role, summary, ok=ok, **detail)


class ArchitectAgent(ContextAgent):
    """Produces (or refreshes) the project plan and records it."""

    __test__ = False
    name = "architect"
    role = "architect"

    def execute(self, request: Any) -> Any:
        from forge.architect import ProjectArchitect
        metadata = request.metadata or {}
        requirement = str(metadata.get("requirement") or request.task.description
                          or "")
        if not requirement.strip():
            return _response(self.name, request, ok=False, output="",
                             error="no requirement supplied")
        profiles = [self.profile] if self.profile is not None else []
        plan = ProjectArchitect(self.root, profiles=profiles).plan(requirement)
        self.record("plan", "plan", plan.summary())
        self.record("specifications", "architecture",
                    [item.to_dict() for item in plan.specifications])
        self.record("architecture", "architecture",
                    [item.to_dict() for item in plan.components])
        self.note("planned %s (%d tasks)" % (plan.requirement.kind,
                                             len(plan.tasks)),
                  kind=plan.requirement.kind, tasks=len(plan.tasks))
        return _response(self.name, request, ok=not plan.validate(),
                         output=plan.render(),
                         error="; ".join(plan.validate()),
                         plan_id=plan.id, kind=plan.requirement.kind,
                         tasks=len(plan.tasks))


class TestAgent(ContextAgent):
    """Runs the staged test pipeline and records the outcome."""

    __test__ = False
    name = "testing"
    role = "testing"

    def execute(self, request: Any) -> Any:
        from forge.testing.engine import TestEngine, render
        metadata = request.metadata or {}
        stages = metadata.get("stages") or ()
        if isinstance(stages, str):
            stages = (stages,)
        engine = TestEngine(self.root, profile=self.profile)
        report = engine.run(stages=tuple(stages))
        summary = report.summary()
        self.record("tests", "tests", summary)
        self.note("%s: %d case(s)" % (summary["status"],
                                      summary["total_cases"]),
                  ok=report.ok, status=summary["status"])
        return _response(self.name, request, ok=report.ok,
                         output=render(report),
                         error="" if report.ok else summary["status"],
                         status=summary["status"],
                         cases=summary["total_cases"])


class DebugAgent(ContextAgent):
    """Runs the bounded debug loop when something is failing."""

    __test__ = False
    name = "debugging"
    role = "debugging"

    def execute(self, request: Any) -> Any:
        from forge.debug.loop import AutomatedDebugLoop, CallableFixStrategy
        from forge.testing.engine import TestEngine
        metadata = request.metadata or {}
        engine = TestEngine(self.root, profile=self.profile)
        strategy = CallableFixStrategy(
            "none", lambda context: [])
        loop = AutomatedDebugLoop(
            self.root,
            tester=lambda targets=None: engine.run(stages=["unit"]),
            strategy=strategy,
            max_iterations=int(metadata.get("max_iterations", 3) or 3))
        report = loop.run()
        summary = report.summary()
        self.record("debug", "diagnostic", summary)
        self.note("debug %s after %d attempt(s)" % (
            summary["outcome"], summary["iterations"]),
            ok=report.fixed, outcome=summary["outcome"])
        return _response(self.name, request, ok=report.fixed,
                         output="debug: %s (%d attempt(s))" % (
                             summary["outcome"], summary["iterations"]),
                         error="" if report.fixed else summary["outcome"],
                         outcome=summary["outcome"],
                         iterations=summary["iterations"])


class PerfAgent(ContextAgent):
    """Benchmarks and records the baseline; never claims an improvement."""

    __test__ = False
    name = "performance"
    role = "performance"

    def execute(self, request: Any) -> Any:
        from forge.perf.lab import PerformanceLab
        metadata = request.metadata or {}
        argv = metadata.get("argv") or []
        if isinstance(argv, str):
            argv = argv.split()
        if not argv:
            return _response(
                self.name, request, ok=False, output="",
                error="no command supplied to benchmark")
        lab = PerformanceLab(self.root, runs=int(metadata.get("runs", 3) or 3))
        result = lab.benchmark(list(argv), label=str(
            metadata.get("label", "run")))
        summary = result.summary()
        self.record("benchmark", "benchmark", summary)
        self.note("benchmarked %s over %d run(s)" % (
            result.name, result.runs), ok=result.succeeded)
        return _response(
            self.name, request, ok=result.succeeded,
            output="benchmark: %d run(s), median wall %s ms" % (
                result.runs,
                summary["stats"]["wall_ms"]["median"]),
            error="" if result.succeeded else "command did not succeed",
            runs=result.runs)


def role_for_capability(capability: str) -> str:
    """Map a Forge capability name onto an engineering role.

    The capability vocabulary already exists
    (:mod:`forge.models.capabilities`); this bridges it to the roster so the
    existing planner can select engineering agents without a second
    vocabulary.
    """
    mapping = {
        "planning": "architect",
        "coding": "coding",
        "testing": "testing",
        "debugging": "debugging",
        "review": "review",
        "security": "security",
        "research": "research",
        "documentation": "documentation",
    }
    return mapping.get(str(capability).strip().lower(), "")
