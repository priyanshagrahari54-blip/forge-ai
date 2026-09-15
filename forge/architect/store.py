"""Plan storage, editing, and the approval gate (A83).

The rule this module exists to enforce: **what was approved is what gets
executed.**

* Plans are persisted as JSON under ``.forge/plans/`` and reloaded byte-for-byte.
* Any edit bumps the revision, records who edited it and why, and drops an
  ``approved`` plan back to ``draft`` — approval is tied to a content
  fingerprint, not to a plan id.
* :meth:`PlanStore.approve` records the fingerprint it approved.
* :meth:`PlanStore.checkout_for_execution` refuses a plan that is not
  approved, that has structural problems, or whose content no longer matches
  the approved fingerprint. The refusal reason is returned, never swallowed.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from forge.architect.models import (
    ArchitectureDecision,
    BenchmarkPlan,
    Component,
    Dependency,
    PlanStatus,
    PlanTask,
    ProjectKind,
    ProjectPlan,
    ReleasePlan,
    Requirement,
    Specification,
    TestPlan,
)

PLANS_DIR = ".forge/plans"
MAX_PLANS = 200
MAX_PLAN_BYTES = 8 * 1024 * 1024
#: Top-level keys a caller may edit. Anything else is a structural change that
#: must go through the Architect, so it is refused here.
EDITABLE_FIELDS = ("specifications", "components", "decisions", "dependencies",
                   "tasks", "test_plan", "benchmark_plan", "release_plan",
                   "open_questions")


class PlanError(ValueError):
    """Raised for an invalid plan operation."""


class PlanNotFound(PlanError):
    """Raised when a plan id does not exist."""


class NotApproved(PlanError):
    """Raised when execution is attempted without approval."""


@dataclass
class Approval:
    """A recorded approval, tied to the exact content approved."""

    plan_id: str
    fingerprint: str
    revision: int
    actor: str
    at: float = field(default_factory=time.time)
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"plan_id": self.plan_id, "fingerprint": self.fingerprint,
                "revision": self.revision, "actor": self.actor, "at": self.at,
                "note": self.note}


# -- (de)serialisation ---------------------------------------------------


def _require(payload: Any, name: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise PlanError("%s must be an object" % name)
    return payload


def _text_list(value: Any, name: str, limit: int = 200) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise PlanError("%s must be a list of strings" % name)
    out: List[str] = []
    for entry in list(value)[:limit]:
        out.append(str(entry))
    return out


def _from_dict(payload: Dict[str, Any]) -> ProjectPlan:
    requirement_payload = _require(payload.get("requirement") or {},
                                   "requirement")
    requirement = Requirement(
        statement=str(requirement_payload.get("statement", "")),
        kind=str(requirement_payload.get("kind",
                                         ProjectKind.UNKNOWN.value)),
        evidence=_text_list(requirement_payload.get("evidence"), "evidence"),
        confidence=float(requirement_payload.get("confidence", 0.0) or 0.0),
        goals=_text_list(requirement_payload.get("goals"), "goals"),
        constraints=_text_list(requirement_payload.get("constraints"),
                               "constraints"),
        acceptance_criteria=_text_list(
            requirement_payload.get("acceptance_criteria"), "criteria"),
        scale=str(requirement_payload.get("scale", "medium")),
        observations=dict(requirement_payload.get("observations") or {}))

    plan = ProjectPlan(
        id=str(payload.get("id", "")), requirement=requirement,
        status=str(payload.get("status", PlanStatus.DRAFT.value)),
        revision=int(payload.get("revision", 1) or 1),
        created_at=float(payload.get("created_at", time.time())),
        updated_at=float(payload.get("updated_at", time.time())),
        profiles=_text_list(payload.get("profiles"), "profiles"),
        open_questions=_text_list(payload.get("open_questions"),
                                  "open_questions"))

    for item in payload.get("specifications") or []:
        item = _require(item, "specification")
        plan.specifications.append(Specification(
            id=str(item.get("id", "")), area=str(item.get("area", "")),
            statement=str(item.get("statement", "")),
            verifiable_by=str(item.get("verifiable_by", "unit-test")),
            rationale=str(item.get("rationale", "")),
            required=bool(item.get("required", True))))
    for item in payload.get("components") or []:
        item = _require(item, "component")
        plan.components.append(Component(
            name=str(item.get("name", "")),
            responsibility=str(item.get("responsibility", "")),
            depends_on=_text_list(item.get("depends_on"), "depends_on"),
            interfaces=_text_list(item.get("interfaces"), "interfaces"),
            path=str(item.get("path", "")), kind=str(item.get("kind", "module"))))
    for item in payload.get("decisions") or []:
        item = _require(item, "decision")
        plan.decisions.append(ArchitectureDecision(
            id=str(item.get("id", "")), title=str(item.get("title", "")),
            decision=str(item.get("decision", "")),
            alternatives=_text_list(item.get("alternatives"), "alternatives"),
            consequences=_text_list(item.get("consequences"), "consequences"),
            status=str(item.get("status", "proposed")),
            source=str(item.get("source", ""))))
    for item in payload.get("dependencies") or []:
        item = _require(item, "dependency")
        plan.dependencies.append(Dependency(
            name=str(item.get("name", "")),
            purpose=str(item.get("purpose", "")),
            required=bool(item.get("required", True)),
            constraint=str(item.get("constraint", "")),
            risk=str(item.get("risk", ""))))
    for item in payload.get("tasks") or []:
        item = _require(item, "task")
        plan.tasks.append(PlanTask(
            id=str(item.get("id", "")), title=str(item.get("title", "")),
            component=str(item.get("component", "")),
            depends_on=_text_list(item.get("depends_on"), "depends_on"),
            acceptance=str(item.get("acceptance", "")),
            role=str(item.get("role", "coding")),
            estimate=str(item.get("estimate", "")),
            status=str(item.get("status", "pending"))))

    test_payload = _require(payload.get("test_plan") or {}, "test_plan")
    plan.test_plan = TestPlan(
        levels=[dict(level) for level in (test_payload.get("levels") or [])
                if isinstance(level, dict)],
        gate=str(test_payload.get("gate", TestPlan().gate)))
    bench_payload = _require(payload.get("benchmark_plan") or {},
                             "benchmark_plan")
    plan.benchmark_plan = BenchmarkPlan(
        metrics=[dict(metric)
                 for metric in (bench_payload.get("metrics") or [])
                 if isinstance(metric, dict)],
        regression_tolerance_percent=float(
            bench_payload.get("regression_tolerance_percent", 2.0) or 2.0),
        min_runs=int(bench_payload.get("min_runs", 3) or 3),
        gate=str(bench_payload.get("gate", BenchmarkPlan().gate)))
    release_payload = _require(payload.get("release_plan") or {},
                               "release_plan")
    plan.release_plan = ReleasePlan(
        steps=[dict(step) for step in (release_payload.get("steps") or [])
               if isinstance(step, dict)],
        rollback=str(release_payload.get("rollback", "")))
    plan.history = [dict(item) for item in (payload.get("history") or [])
                    if isinstance(item, dict)]
    return plan


class PlanStore:
    """Persists, edits, approves, and releases project plans."""

    def __init__(self, root: str | Path = ".", *,
                 directory: str = PLANS_DIR) -> None:
        self.root = Path(root).resolve()
        self.directory = self.root / directory
        self.approvals: Dict[str, Approval] = {}

    # -- storage ---------------------------------------------------------

    def _path(self, plan_id: str) -> Path:
        safe = "".join(char for char in (plan_id or "")
                       if char.isalnum() or char in "-_")
        if not safe:
            raise PlanError("a plan id is required")
        return self.directory / ("%s.json" % safe)

    def save(self, plan: ProjectPlan) -> Path:
        path = self._path(plan.id)
        payload = json.dumps(plan.to_dict(), indent=2, sort_keys=True)
        if len(payload.encode("utf-8")) > MAX_PLAN_BYTES:
            raise PlanError("plan exceeds the %d-byte bound" % MAX_PLAN_BYTES)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        self._prune()
        return path

    def load(self, plan_id: str) -> ProjectPlan:
        path = self._path(plan_id)
        if not path.is_file():
            raise PlanNotFound("no such plan: %s" % plan_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PlanError("plan %s is unreadable: %s" % (plan_id, exc)) from exc
        return _from_dict(_require(payload, "plan"))

    def list(self) -> List[Dict[str, Any]]:
        if not self.directory.is_dir():
            return []
        out: List[Dict[str, Any]] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                plan = _from_dict(_require(
                    json.loads(path.read_text(encoding="utf-8")), "plan"))
            except (OSError, ValueError, PlanError):
                out.append({"id": path.stem, "error": "unreadable"})
                continue
            out.append(plan.summary())
        return out

    def delete(self, plan_id: str) -> bool:
        path = self._path(plan_id)
        self.approvals.pop(plan_id, None)
        try:
            path.unlink()
            return True
        except OSError:
            return False

    def _prune(self) -> None:
        paths = sorted(self.directory.glob("*.json"),
                       key=lambda item: item.stat().st_mtime)
        for path in paths[:-MAX_PLANS]:
            try:
                path.unlink()
            except OSError:
                continue

    # -- editing ---------------------------------------------------------

    def update(self, plan_id: str, changes: Dict[str, Any], *,
               actor: str = "", summary: str = "") -> ProjectPlan:
        """Apply an edit, re-validate, and invalidate any approval."""
        if not isinstance(changes, dict) or not changes:
            raise PlanError("changes must be a non-empty object")
        unknown = sorted(set(changes) - set(EDITABLE_FIELDS))
        if unknown:
            raise PlanError(
                "these fields cannot be edited directly: %s" % ", ".join(unknown))
        plan = self.load(plan_id)
        if plan.status in (PlanStatus.COMPLETED.value,
                           PlanStatus.ABANDONED.value):
            raise PlanError("a %s plan cannot be edited" % plan.status)
        self._apply(plan, changes)
        plan.record_edit(actor or "unknown",
                         summary or "edited %s" % ", ".join(sorted(changes)))
        self.save(plan)
        # An approval covered the previous content; it no longer applies.
        self.approvals.pop(plan_id, None)
        return plan

    @staticmethod
    def _apply(plan: ProjectPlan, changes: Dict[str, Any]) -> None:
        for key, value in changes.items():
            if key == "specifications":
                plan.specifications = [
                    Specification(
                        id=str(item.get("id", "")),
                        area=str(item.get("area", "")),
                        statement=str(item.get("statement", "")),
                        verifiable_by=str(item.get("verifiable_by",
                                                   "unit-test")),
                        rationale=str(item.get("rationale", "")),
                        required=bool(item.get("required", True)))
                    for item in _list_of(value, key)]
            elif key == "components":
                plan.components = [
                    Component(
                        name=str(item.get("name", "")),
                        responsibility=str(item.get("responsibility", "")),
                        depends_on=_text_list(item.get("depends_on"), key),
                        interfaces=_text_list(item.get("interfaces"), key),
                        path=str(item.get("path", "")),
                        kind=str(item.get("kind", "module")))
                    for item in _list_of(value, key)]
            elif key == "decisions":
                plan.decisions = [
                    ArchitectureDecision(
                        id=str(item.get("id", "")),
                        title=str(item.get("title", "")),
                        decision=str(item.get("decision", "")),
                        alternatives=_text_list(item.get("alternatives"), key),
                        consequences=_text_list(item.get("consequences"), key),
                        status=str(item.get("status", "proposed")),
                        source=str(item.get("source", "")))
                    for item in _list_of(value, key)]
            elif key == "dependencies":
                plan.dependencies = [
                    Dependency(
                        name=str(item.get("name", "")),
                        purpose=str(item.get("purpose", "")),
                        required=bool(item.get("required", True)),
                        constraint=str(item.get("constraint", "")),
                        risk=str(item.get("risk", "")))
                    for item in _list_of(value, key)]
            elif key == "tasks":
                plan.tasks = [
                    PlanTask(
                        id=str(item.get("id", "")),
                        title=str(item.get("title", "")),
                        component=str(item.get("component", "")),
                        depends_on=_text_list(item.get("depends_on"), key),
                        acceptance=str(item.get("acceptance", "")),
                        role=str(item.get("role", "coding")),
                        estimate=str(item.get("estimate", "")),
                        status=str(item.get("status", "pending")))
                    for item in _list_of(value, key)]
            elif key == "test_plan":
                payload = _require(value, key)
                plan.test_plan = TestPlan(
                    levels=[dict(level)
                            for level in (payload.get("levels") or [])
                            if isinstance(level, dict)],
                    gate=str(payload.get("gate", plan.test_plan.gate)))
            elif key == "benchmark_plan":
                payload = _require(value, key)
                plan.benchmark_plan = BenchmarkPlan(
                    metrics=[dict(metric)
                             for metric in (payload.get("metrics") or [])
                             if isinstance(metric, dict)],
                    regression_tolerance_percent=float(
                        payload.get("regression_tolerance_percent",
                                    plan.benchmark_plan
                                    .regression_tolerance_percent) or 2.0),
                    min_runs=int(payload.get("min_runs",
                                             plan.benchmark_plan.min_runs) or 3),
                    gate=str(payload.get("gate",
                                         plan.benchmark_plan.gate)))
            elif key == "release_plan":
                payload = _require(value, key)
                plan.release_plan = ReleasePlan(
                    steps=[dict(step)
                           for step in (payload.get("steps") or [])
                           if isinstance(step, dict)],
                    rollback=str(payload.get("rollback", "")))
            elif key == "open_questions":
                plan.open_questions = _text_list(value, key)

    # -- approval and execution ------------------------------------------

    def approve(self, plan_id: str, *, actor: str = "", note: str = "",
                ignore_problems: bool = False) -> Approval:
        """Approve the plan exactly as it is right now."""
        plan = self.load(plan_id)
        problems = plan.validate()
        if problems and not ignore_problems:
            raise PlanError(
                "the plan has problems that must be resolved first: %s"
                % "; ".join(problems))
        who = actor or "unknown"
        # record_edit first: it bumps the revision, appends history and
        # resets the status to draft. The approval has to describe the plan
        # as it will actually be stored, so the fingerprint is taken *after*
        # every mutation — otherwise it can never match at checkout time.
        plan.record_edit(who, "approved by %s" % who)
        plan.status = PlanStatus.APPROVED.value
        self.save(plan)
        approval = Approval(plan_id=plan.id, fingerprint=plan.fingerprint(),
                            revision=plan.revision, actor=who, note=note)
        self.approvals[plan.id] = approval
        return approval

    def approval(self, plan_id: str) -> Optional[Approval]:
        return self.approvals.get(plan_id)

    def checkout_for_execution(self, plan_id: str) -> ProjectPlan:
        """Return the plan for execution, or explain why it may not run."""
        plan = self.load(plan_id)
        approval = self.approvals.get(plan_id)
        if approval is None:
            raise NotApproved(
                "plan %s has not been approved; nothing will be executed"
                % plan_id)
        if approval.fingerprint != plan.fingerprint():
            raise NotApproved(
                "plan %s changed after approval (approved %s, now %s); "
                "re-approve before executing"
                % (plan_id, approval.fingerprint, plan.fingerprint()))
        problems = plan.validate()
        if problems:
            raise NotApproved(
                "plan %s has structural problems: %s"
                % (plan_id, "; ".join(problems)))
        plan.status = PlanStatus.EXECUTING.value
        self.save(plan)
        return plan

    def complete(self, plan_id: str) -> ProjectPlan:
        plan = self.load(plan_id)
        plan.status = PlanStatus.COMPLETED.value
        plan.record_edit("forge", "marked completed")
        self.save(plan)
        return plan

    def abandon(self, plan_id: str, *, reason: str = "") -> ProjectPlan:
        plan = self.load(plan_id)
        plan.status = PlanStatus.ABANDONED.value
        plan.record_edit("forge", "abandoned: %s" % (reason or "no reason given"))
        self.approvals.pop(plan_id, None)
        self.save(plan)
        return plan


def _list_of(value: Any, name: str) -> List[Dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        raise PlanError("%s must be a list of objects" % name)
    out: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise PlanError("%s entries must be objects" % name)
        out.append(item)
    return out
