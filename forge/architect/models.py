"""Project Architect data model (A83).

The plan is the contract between a human requirement and Forge's engines. Every
element carries the evidence or rationale that produced it, so a reviewer can
tell a reasoned decision from a template filler — and can edit any of it
before anything is executed.

Nothing in this module runs anything. It is pure structure.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

MAX_REQUIREMENT_CHARS = 20_000
MAX_ITEMS = 200
MAX_TEXT = 4_000


class PlanStatus(str, Enum):
    """Lifecycle of a project plan.

    ``approved`` is only ever reached through :meth:`forge.architect.store.
    PlanStore.approve`, and any edit drops an approved plan back to ``draft``:
    what was approved must be what gets executed.
    """

    DRAFT = "draft"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class ProjectKind(str, Enum):
    """The shapes of project the Architect knows how to plan."""

    SMALL_PROJECT = "small-project"
    WEB_APPLICATION = "web-application"
    API_SERVICE = "api-service"
    LIBRARY = "library"
    CLI_TOOL = "cli-tool"
    DESKTOP_APPLICATION = "desktop-application"
    MOBILE_APPLICATION = "mobile-application"
    AI_APPLICATION = "ai-application"
    OPERATING_SYSTEM = "operating-system"
    KERNEL_MODULE = "kernel-module"
    DRIVER = "driver"
    SYSTEM_SOFTWARE = "system-software"
    UNKNOWN = "unknown"


@dataclass
class Requirement:
    """What the user asked for, classified and made explicit."""

    statement: str
    kind: str = ProjectKind.UNKNOWN.value
    #: Terms in the statement that produced the classification.
    evidence: List[str] = field(default_factory=list)
    confidence: float = 0.0
    #: Explicit goals, extracted or stated.
    goals: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    #: What "done" means, in checkable terms.
    acceptance_criteria: List[str] = field(default_factory=list)
    #: Scale hint: small | medium | large.
    scale: str = "medium"
    #: Repository facts the Architect actually observed.
    observations: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "statement": self.statement, "kind": self.kind,
            "evidence": list(self.evidence),
            "confidence": round(self.confidence, 3),
            "goals": list(self.goals), "constraints": list(self.constraints),
            "acceptance_criteria": list(self.acceptance_criteria),
            "scale": self.scale, "observations": dict(self.observations),
            "created_at": self.created_at,
        }


@dataclass
class Specification:
    """One verifiable statement about what the project must do."""

    id: str
    area: str
    statement: str
    #: How this will be checked: a test level, a benchmark, a review.
    verifiable_by: str = "unit-test"
    rationale: str = ""
    required: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "area": self.area,
                "statement": self.statement,
                "verifiable_by": self.verifiable_by,
                "rationale": self.rationale, "required": self.required}


@dataclass
class Component:
    """One unit of the architecture."""

    name: str
    responsibility: str
    depends_on: List[str] = field(default_factory=list)
    #: Public interface the component promises.
    interfaces: List[str] = field(default_factory=list)
    #: Where it lives, once decided.
    path: str = ""
    kind: str = "module"

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "responsibility": self.responsibility,
                "depends_on": list(self.depends_on),
                "interfaces": list(self.interfaces), "path": self.path,
                "kind": self.kind}


@dataclass
class ArchitectureDecision:
    """A recorded decision with its alternatives and consequences."""

    id: str
    title: str
    decision: str
    alternatives: List[str] = field(default_factory=list)
    consequences: List[str] = field(default_factory=list)
    status: str = "proposed"       # proposed | accepted | superseded
    source: str = ""               # profile | detection | convention

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "title": self.title, "decision": self.decision,
                "alternatives": list(self.alternatives),
                "consequences": list(self.consequences),
                "status": self.status, "source": self.source}


@dataclass
class Dependency:
    """An external dependency the plan intends to take on."""

    name: str
    purpose: str
    required: bool = True
    #: Constraint, e.g. ">=2.0,<3" — recorded, never auto-installed.
    constraint: str = ""
    risk: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "purpose": self.purpose,
                "required": self.required, "constraint": self.constraint,
                "risk": self.risk}


@dataclass
class PlanTask:
    """One unit of implementation work."""

    id: str
    title: str
    component: str = ""
    depends_on: List[str] = field(default_factory=list)
    #: Acceptance condition for this task alone.
    acceptance: str = ""
    #: Engineering role that should own it (see ``forge.engineering.roster``).
    role: str = "coding"
    estimate: str = ""
    status: str = "pending"        # pending | running | done | blocked

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "title": self.title, "component": self.component,
                "depends_on": list(self.depends_on), "acceptance": self.acceptance,
                "role": self.role, "estimate": self.estimate,
                "status": self.status}


@dataclass
class TestPlan:
    """What will be tested, at which level, and what counts as passing."""

    levels: List[Dict[str, Any]] = field(default_factory=list)
    #: Gate rule, stated plainly.
    gate: str = ("Every required level must run and pass; a level that "
                 "collected no tests does not count as a pass.")

    def add_level(self, name: str, scope: str, command: str = "",
                  required: bool = True, notes: str = "") -> None:
        self.levels.append({"name": name, "scope": scope, "command": command,
                            "required": required, "notes": notes})

    def to_dict(self) -> Dict[str, Any]:
        return {"levels": list(self.levels), "gate": self.gate}


@dataclass
class BenchmarkPlan:
    """What will be measured, against which baseline and threshold."""

    metrics: List[Dict[str, Any]] = field(default_factory=list)
    #: Relative regression beyond this is rejected.
    regression_tolerance_percent: float = 2.0
    min_runs: int = 3
    gate: str = ("No metric may regress beyond the tolerance across at least "
                 "the minimum number of runs; improvements are only claimed "
                 "when measured.")

    def add_metric(self, name: str, how: str, target: str = "",
                   higher_is_better: bool = False) -> None:
        self.metrics.append({"name": name, "how": how, "target": target,
                             "higher_is_better": higher_is_better})

    def to_dict(self) -> Dict[str, Any]:
        return {"metrics": list(self.metrics),
                "regression_tolerance_percent": self.regression_tolerance_percent,
                "min_runs": self.min_runs, "gate": self.gate}


@dataclass
class ReleasePlan:
    """How the work becomes something a user can run."""

    steps: List[Dict[str, Any]] = field(default_factory=list)
    #: Rollback is part of the plan, not an afterthought.
    rollback: str = ""

    def add_step(self, name: str, detail: str = "", gate: str = "") -> None:
        self.steps.append({"name": name, "detail": detail, "gate": gate})

    def to_dict(self) -> Dict[str, Any]:
        return {"steps": list(self.steps), "rollback": self.rollback}


@dataclass
class ProjectPlan:
    """The whole plan, editable, versioned, and approval-gated."""

    id: str
    requirement: Requirement
    specifications: List[Specification] = field(default_factory=list)
    components: List[Component] = field(default_factory=list)
    decisions: List[ArchitectureDecision] = field(default_factory=list)
    dependencies: List[Dependency] = field(default_factory=list)
    tasks: List[PlanTask] = field(default_factory=list)
    test_plan: TestPlan = field(default_factory=TestPlan)
    benchmark_plan: BenchmarkPlan = field(default_factory=BenchmarkPlan)
    release_plan: ReleasePlan = field(default_factory=ReleasePlan)
    status: str = PlanStatus.DRAFT.value
    revision: int = 1
    #: One entry per edit, oldest first.
    history: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    #: Profile(s) that shaped this plan.
    profiles: List[str] = field(default_factory=list)
    #: Open questions the Architect could not answer from evidence.
    open_questions: List[str] = field(default_factory=list)

    # -- accessors -------------------------------------------------------

    def task(self, task_id: str) -> Optional[PlanTask]:
        for item in self.tasks:
            if item.id == task_id:
                return item
        return None

    def component(self, name: str) -> Optional[Component]:
        for item in self.components:
            if item.name == name:
                return item
        return None

    def ready_tasks(self) -> List[PlanTask]:
        """Tasks whose dependencies are all done — a real execution frontier.

        An invalid plan has no frontier. Returning work from a plan whose
        requirement is empty or whose graph is cyclic would let an executor
        start on something that has not actually been specified, so structural
        problems empty the frontier instead of being reported elsewhere.
        """
        if self.validate():
            return []
        done = {item.id for item in self.tasks if item.status == "done"}
        return [item for item in self.tasks
                if item.status == "pending"
                and all(dep in done for dep in item.depends_on)]

    def validate(self) -> List[str]:
        """Structural problems that would make execution meaningless."""
        problems: List[str] = []
        if not self.requirement.statement.strip():
            problems.append("the requirement is empty")
        ids = [item.id for item in self.tasks]
        if len(ids) != len(set(ids)):
            problems.append("duplicate task ids")
        known = set(ids)
        for item in self.tasks:
            for dep in item.depends_on:
                if dep not in known:
                    problems.append(
                        "task %s depends on unknown task %s" % (item.id, dep))
            if item.id in item.depends_on:
                problems.append("task %s depends on itself" % item.id)
        components = {item.name for item in self.components}
        for item in self.components:
            for dep in item.depends_on:
                if dep not in components:
                    problems.append(
                        "component %s depends on unknown component %s"
                        % (item.name, dep))
        if self._has_cycle():
            problems.append("the task graph contains a cycle")
        if not self.tasks:
            problems.append("the plan has no implementation tasks")
        if not self.test_plan.levels:
            problems.append("the plan has no test levels")
        return problems

    def _has_cycle(self) -> bool:
        state: Dict[str, int] = {}
        graph = {item.id: list(item.depends_on) for item in self.tasks}

        def visit(node: str) -> bool:
            if state.get(node) == 1:
                return True
            if state.get(node) == 2:
                return False
            state[node] = 1
            for dep in graph.get(node, ()):
                if visit(dep):
                    return True
            state[node] = 2
            return False

        return any(visit(node) for node in graph)

    # -- editing ---------------------------------------------------------

    def record_edit(self, actor: str, summary: str) -> None:
        self.revision += 1
        self.updated_at = time.time()
        self.history.append({"revision": self.revision, "actor": actor,
                             "summary": summary[:MAX_TEXT],
                             "at": self.updated_at})
        self.history = self.history[-MAX_ITEMS:]
        # An edit invalidates approval: the approved artifact changed.
        if self.status == PlanStatus.APPROVED.value:
            self.status = PlanStatus.DRAFT.value

    def fingerprint(self) -> str:
        """Content hash, so an approval can be tied to an exact plan.

        Two things are excluded deliberately, and both are about making the
        hash mean "this plan" rather than "this moment":

        * the fingerprint field itself, which would otherwise recurse through
          :meth:`to_dict`;
        * wall-clock metadata (``created_at``, ``updated_at``). Timestamps are
          regenerated on deserialisation, so including them made every reload
          look like an edit and invalidated approvals that had not changed.
        """
        import json
        payload = json.dumps(
            self._content_hash_payload(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _content_hash_payload(self) -> Dict[str, Any]:
        """The plan's *content*, with volatile metadata stripped."""
        payload = self._payload(include_history=False)
        for key in ("created_at", "updated_at"):
            payload.pop(key, None)
        requirement = payload.get("requirement")
        if isinstance(requirement, dict):
            requirement = dict(requirement)
            requirement.pop("created_at", None)
            requirement.pop("updated_at", None)
            payload["requirement"] = requirement
        return payload

    # -- serialisation ---------------------------------------------------

    def _payload(self, *, include_history: bool) -> Dict[str, Any]:
        """The plan's serialisable content, minus the derived fingerprint."""
        payload: Dict[str, Any] = {
            "id": self.id,
            "requirement": self.requirement.to_dict(),
            "specifications": [item.to_dict() for item in self.specifications],
            "components": [item.to_dict() for item in self.components],
            "decisions": [item.to_dict() for item in self.decisions],
            "dependencies": [item.to_dict() for item in self.dependencies],
            "tasks": [item.to_dict() for item in self.tasks],
            "test_plan": self.test_plan.to_dict(),
            "benchmark_plan": self.benchmark_plan.to_dict(),
            "release_plan": self.release_plan.to_dict(),
            "status": self.status,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "profiles": list(self.profiles),
            "open_questions": list(self.open_questions),
            "problems": self.validate(),
        }
        if include_history:
            payload["history"] = list(self.history)
        return payload

    def to_dict(self, *, include_history: bool = True) -> Dict[str, Any]:
        payload = self._payload(include_history=include_history)
        payload["fingerprint"] = self.fingerprint()
        return payload

    def summary(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.requirement.kind,
            "status": self.status,
            "revision": self.revision,
            "fingerprint": self.fingerprint(),
            "specifications": len(self.specifications),
            "components": len(self.components),
            "decisions": len(self.decisions),
            "dependencies": len(self.dependencies),
            "tasks": len(self.tasks),
            "ready_tasks": [item.id for item in self.ready_tasks()],
            "test_levels": [level["name"]
                            for level in self.test_plan.levels],
            "benchmarks": [metric["name"]
                           for metric in self.benchmark_plan.metrics],
            "release_steps": [step["name"]
                              for step in self.release_plan.steps],
            "profiles": list(self.profiles),
            "open_questions": list(self.open_questions),
            "problems": self.validate(),
        }

    def render(self) -> str:
        """Human-readable plan — what a reviewer reads before approving."""
        lines = [
            "# Plan %s (rev %d, %s)" % (self.id, self.revision, self.status),
            "",
            "Requirement: %s" % self.requirement.statement,
            "Classification: %s (confidence %.2f, evidence: %s)" % (
                self.requirement.kind, self.requirement.confidence,
                ", ".join(self.requirement.evidence) or "none"),
            "",
            "## Specifications",
        ]
        for item in self.specifications:
            lines.append("- %s [%s] %s (check: %s)" % (
                item.id, item.area, item.statement, item.verifiable_by))
        lines += ["", "## Architecture"]
        for item in self.components:
            deps = ", ".join(item.depends_on) or "none"
            lines.append("- %s (%s): %s [deps: %s]" % (
                item.name, item.kind, item.responsibility, deps))
        if self.decisions:
            lines += ["", "## Decisions"]
            for item in self.decisions:
                lines.append("- %s %s: %s (%s)" % (
                    item.id, item.title, item.decision, item.source or "n/a"))
        if self.dependencies:
            lines += ["", "## Dependencies"]
            for item in self.dependencies:
                lines.append("- %s%s — %s" % (
                    item.name,
                    " (%s)" % item.constraint if item.constraint else "",
                    item.purpose))
        lines += ["", "## Implementation"]
        for item in self.tasks:
            deps = ", ".join(item.depends_on) or "-"
            lines.append("- %s [%s, deps: %s] %s — %s" % (
                item.id, item.role, deps, item.title, item.acceptance))
        lines += ["", "## Testing"]
        for level in self.test_plan.levels:
            lines.append("- %s%s: %s" % (
                level["name"], "" if level["required"] else " (advisory)",
                level["scope"]))
        lines += ["", "## Benchmarking"]
        for metric in self.benchmark_plan.metrics:
            lines.append("- %s: %s" % (metric["name"], metric["how"]))
        lines += ["", "## Release"]
        for step in self.release_plan.steps:
            lines.append("- %s%s" % (
                step["name"],
                " — %s" % step["detail"] if step["detail"] else ""))
        problems = self.validate()
        if problems:
            lines += ["", "## Problems that must be resolved"]
            lines.extend("- %s" % problem for problem in problems)
        if self.open_questions:
            lines += ["", "## Open questions"]
            lines.extend("- %s" % question for question in self.open_questions)
        return "\n".join(lines)
