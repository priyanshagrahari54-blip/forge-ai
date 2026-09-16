"""AutoPilot (A84): hand Forge a complex project, and it completes it.

The pieces already existed but were never joined into one autonomous
transaction:

* the Project Architect (A83) turns a requirement into a reviewable plan;
* the PlanStore gates execution behind an explicit approval;
* the Supervisor (A32) executes one requirement through the full guarded
  loop (model -> code -> test/debug -> review -> security -> acceptance
  -> checkpoint -> commit).

The AutoPilot closes the loop:

    requirement -> plan -> approve -> checkout -> frontier loop ->
        per-task execution through the guarded engines -> progress
        persisted after every task -> all tasks done -> plan completed

Rules the AutoPilot never breaks:

* **What was approved is what gets executed.** Execution starts only after
  :meth:`PlanStore.checkout_for_execution` accepts the approved
  fingerprint. On resume, the plan content must still match the approved
  fingerprint once task *statuses* (execution bookkeeping, not scope) are
  normalised away; any real edit invalidates the run.
* **Every write goes through the Supervisor transaction.** The AutoPilot
  itself writes nothing into the target tree except through the existing
  guarded pipeline; its own bookkeeping lives under ``.forge/autopilot/``
  and in the plan store.
* **No invented success.** A verification task (review, security, release)
  completes only on a recorded gate verdict; a coding task completes only
  on an accepted Supervisor run. Failures become bounded retries with the
  failure reason fed back, then ``blocked`` — never silent skips.
* **Progress survives interruption.** Plan task statuses are persisted in
  the plan file and attempt history under ``.forge/autopilot/`` after
  every task, so a run can be resumed exactly where it stopped.

Python floor: 3.8.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from forge.architect.models import PlanStatus, PlanTask, ProjectPlan
from forge.architect.store import PlanError, PlanStore
from forge.security.permissions import OperationMode

AUTOPILOT_DIR = ".forge/autopilot"
#: Git's well-known empty-tree object; the diff base for repos with no
#: commits yet. Never a user-facing magic string: it is what ``git diff``
#: itself compares against when nothing has been committed.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

#: Roles whose plan task is an inspection/evidence step; every other role
#: (coding, documentation, hardware, …) is expected to change files and
#: goes through the full Supervisor transaction.
REVIEW_ROLE = "review"
SECURITY_ROLE = "security"
PERFORMANCE_ROLE = "performance"
RELEASE_ROLE = "release"
RESEARCH_ROLE = "research"
ARCHITECT_ROLE = "architect"

MAX_TASK_REQUIREMENT_CHARS = 8000
MAX_FAILURE_CONTEXT_CHARS = 1500
MAX_EVIDENCE_CHARS = 2000
#: Hard bound on benchmark repetitions for the baseline step; the plan's
#: own ``min_runs`` is honoured below this cap.
MAX_BENCHMARK_RUNS = 3

STATUS_COMPLETED = "completed"
STATUS_BLOCKED = "blocked"
STATUS_REFUSED = "refused"
STATUS_ERROR = "error"


class AutoPilotError(RuntimeError):
    """Raised for invalid AutoPilot usage that cannot run at all."""


@dataclass
class TaskOutcome:
    """The measured outcome of one plan task."""

    task_id: str
    title: str = ""
    role: str = ""
    status: str = "pending"          # done | blocked
    attempts: int = 0
    error: str = ""
    files: Tuple[str, ...] = ()
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id, "title": self.title, "role": self.role,
            "status": self.status, "attempts": self.attempts,
            "error": self.error, "files": list(self.files),
            "evidence": dict(self.evidence),
        }


@dataclass
class AutoReport:
    """The honest outcome of one AutoPilot transaction."""

    status: str                      # completed | blocked | refused | error
    plan_id: str = ""
    requirement: str = ""
    reason: str = ""
    tasks: List[TaskOutcome] = field(default_factory=list)
    files_changed: Tuple[str, ...] = ()
    commits: int = 0
    start_commit: str = ""
    duration_seconds: float = 0.0
    resumed: bool = False
    plan_summary: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_COMPLETED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "plan_id": self.plan_id,
            "requirement": self.requirement,
            "reason": self.reason,
            "resumed": self.resumed,
            "tasks": [item.to_dict() for item in self.tasks],
            "files_changed": list(self.files_changed),
            "commits": self.commits,
            "start_commit": self.start_commit,
            "duration_seconds": round(self.duration_seconds, 3),
            "plan_summary": dict(self.plan_summary),
        }

    def render(self) -> str:
        lines = ["AutoPilot: %s (plan %s)" % (self.status, self.plan_id or "-")]
        if self.reason:
            lines.append("reason: %s" % self.reason)
        for item in self.tasks:
            detail = ""
            if item.status == "blocked":
                detail = " — %s" % (item.error or "blocked")[:200]
            elif item.files:
                detail = " — %d file(s)" % len(item.files)
            lines.append("  %-5s %-13s %-9s %d attempt(s)%s" % (
                item.task_id, item.role, item.status, item.attempts, detail))
        if self.files_changed:
            shown = list(self.files_changed)[:12]
            more = len(self.files_changed) - len(shown)
            lines.append("files changed: %s%s" % (
                ", ".join(shown),
                " (+%d more)" % more if more > 0 else ""))
        if self.commits:
            lines.append("commits: %d" % self.commits)
        lines.append("duration: %.1fs" % self.duration_seconds)
        return "\n".join(lines)


def _execution_fingerprint(plan: ProjectPlan) -> str:
    """Fingerprint of the plan with execution bookkeeping reset.

    Two kinds of field move while a plan executes, and both are
    bookkeeping, not scope:

    * task statuses change after every completed task;
    * the plan status itself moves ``approved -> executing -> completed``.

    The approval fingerprint was taken with tasks ``pending`` and the plan
    ``approved``, so both are reset before comparing. Everything else —
    the requirement, specifications, components, task *definitions*, test
    and release plans — is scope, and any change to it invalidates the
    approval.
    """
    from forge.architect.store import _from_dict

    payload = plan.to_dict()
    payload["status"] = PlanStatus.APPROVED.value
    for item in payload.get("tasks") or []:
        if isinstance(item, dict):
            item["status"] = "pending"
    return _from_dict(payload).fingerprint()


class AutoPilot:
    """Drives an approved project plan to completion, task by task.

    The AutoPilot is deliberately thin: planning lives in the Architect,
    approval in the PlanStore, and every repository write in the
    Supervisor. This class only sequences them, persists progress, and
    reports honestly.
    """

    def __init__(self, root: str | Path = ".", *,
                 project_id: str = "autopilot",
                 fabric: Any = None,
                 mode: OperationMode = OperationMode.AUTONOMOUS,
                 max_task_retries: int = 1,
                 max_debug_retries: int = 3,
                 actor: str = "operator",
                 profiles: Sequence[Any] = ()) -> None:
        self.root = Path(root).resolve()
        self.project_id = str(project_id or "autopilot")
        self.fabric = fabric
        self.mode = OperationMode(mode)
        if self.mode in (OperationMode.SAFE, OperationMode.LOCKED):
            raise AutoPilotError(
                "AutoPilot cannot complete a project in %s mode; it needs "
                "write permission (autonomous or assisted)."
                % self.mode.value)
        #: Retries *after* the first attempt for each task.
        self.max_task_retries = max(0, min(5, int(max_task_retries)))
        self.max_debug_retries = max(0, int(max_debug_retries))
        self.actor = str(actor or "operator")
        self.profiles = list(profiles)

    # -- public entry points ---------------------------------------------

    def run(self, requirement: str, *, dry_run: bool = False,
            force: bool = False, on_event: Any = None) -> AutoReport:
        """Plan, approve, and execute a complex project requirement.

        With ``dry_run`` the plan is produced and stored for review but
        nothing is approved or executed.
        """
        started = time.time()
        text = str(requirement or "").strip()
        if not text:
            return AutoReport(status=STATUS_REFUSED,
                              reason="the requirement is empty")
        refusal = self._preflight(force=force, dry_run=dry_run)
        if refusal is not None:
            return AutoReport(status=STATUS_REFUSED, requirement=text,
                              reason=refusal)

        from forge.architect import ProjectArchitect

        architect = ProjectArchitect(self.root, profiles=self.profiles)
        plan = architect.plan(text)
        store = PlanStore(self.root)
        store.save(plan)
        self._emit(on_event, "plan_created", {
            "plan_id": plan.id, "kind": plan.requirement.kind,
            "tasks": len(plan.tasks)})
        if dry_run:
            return AutoReport(status=STATUS_REFUSED, plan_id=plan.id,
                              requirement=text,
                              reason="dry run: plan stored for review; "
                                     "nothing approved or executed",
                              plan_summary=plan.summary(),
                              duration_seconds=time.time() - started)
        # The approval gate: AutoPilot is a delegated operator, so the
        # approval names the actor and is tied to the plan's fingerprint.
        # A structurally invalid plan is refused here, before anything runs.
        try:
            store.approve(
                plan.id, actor=self.actor,
                note="autopilot delegated execution of this plan")
        except PlanError as exc:
            return AutoReport(status=STATUS_REFUSED, plan_id=plan.id,
                              requirement=text,
                              reason="plan cannot be approved: %s" % exc,
                              plan_summary=plan.summary(),
                              duration_seconds=time.time() - started)
        plan = store.load(plan.id)
        return self._execute_plan(store, plan, on_event=on_event,
                                  started=started, resumed=False)

    def resume(self, plan_id: str, *, force: bool = False,
               on_event: Any = None) -> AutoReport:
        """Continue an interrupted (or blocked) execution of an approved plan."""
        started = time.time()
        store = PlanStore(self.root)
        try:
            plan = store.load(plan_id)
        except PlanError as exc:
            return AutoReport(status=STATUS_REFUSED, plan_id=plan_id,
                              reason="cannot load plan: %s" % exc)
        # Plan-state refusals first: they are local and deterministic, so
        # an invalid plan is reported as a plan problem, never hidden
        # behind model diagnostics.
        if plan.status == PlanStatus.DRAFT.value:
            return AutoReport(
                status=STATUS_REFUSED, plan_id=plan_id,
                requirement=plan.requirement.statement,
                reason="plan %s is a draft; approve it before executing "
                       "(forge engineer approve %s)" % (plan_id, plan_id))
        if plan.status == PlanStatus.COMPLETED.value:
            return AutoReport(
                status=STATUS_REFUSED, plan_id=plan_id,
                requirement=plan.requirement.statement,
                reason="plan %s is already completed" % plan_id,
                plan_summary=plan.summary())
        if plan.status == PlanStatus.ABANDONED.value:
            return AutoReport(
                status=STATUS_REFUSED, plan_id=plan_id,
                requirement=plan.requirement.statement,
                reason="plan %s was abandoned" % plan_id)

        approval = store.approval(plan_id)
        if approval is None:
            return AutoReport(
                status=STATUS_REFUSED, plan_id=plan_id,
                requirement=plan.requirement.statement,
                reason="plan %s has no recorded approval; approve it first"
                       % plan_id)
        # The gate: scope must match what was approved. Task statuses are
        # execution bookkeeping and are normalised before comparing.
        if plan.status == PlanStatus.EXECUTING.value:
            if _execution_fingerprint(plan) != approval.fingerprint:
                return AutoReport(
                    status=STATUS_REFUSED, plan_id=plan_id,
                    requirement=plan.requirement.statement,
                    reason="plan %s was edited after approval; re-approve "
                           "before executing" % plan_id)

        refusal = self._preflight(force=force)
        if refusal is not None:
            return AutoReport(status=STATUS_REFUSED, plan_id=plan_id,
                              requirement=plan.requirement.statement,
                              reason=refusal)
        if plan.status == PlanStatus.APPROVED.value:
            try:  # approved but never started
                plan = store.checkout_for_execution(plan_id)
            except PlanError as exc:
                return AutoReport(status=STATUS_REFUSED, plan_id=plan_id,
                                  requirement=plan.requirement.statement,
                                  reason=str(exc))
        # Blocked tasks from earlier passes are worth one more try: the
        # usual reason to resume is that something outside the plan was
        # fixed. Pending stays pending; done stays done.
        retrying: List[str] = []
        for task in plan.tasks:
            if task.status == "blocked":
                task.status = "pending"
                retrying.append(task.id)
        if retrying:
            store.save(plan)
            self._emit(on_event, "tasks_unblocked", {"tasks": retrying})
        report = self._execute_plan(store, plan, on_event=on_event,
                                    started=started, resumed=True)
        return report

    # -- pre-flight --------------------------------------------------------

    def _preflight(self, *, force: bool, dry_run: bool = False
                   ) -> Optional[str]:
        """Return a refusal reason, or None when the run may proceed.

        Mirrors the ``forge run`` gates: AutoPilot without a real code
        model is a doomed pipeline, so it fails fast and explains itself
        instead of ending in "Model proposed no changes". A ``dry_run``
        only produces a reviewable plan, so the static no-model gate still
        applies but the live reachability probe is skipped — a plan can be
        reviewed now and executed once the model is up.
        """
        if not self.root.is_dir():
            return ("project root %s does not exist (create it, or pass an "
                    "existing repository)" % self.root)
        if not (self.root / ".git").is_dir():
            return ("project root %s is not a git repository; Forge commits "
                    "each accepted task, so run `git init` there first (or "
                    "use `forge auto --init-git`)" % self.root)
        fabric = self._fabric_or_default()
        from forge.models.readiness import (
            check_fabric_readiness, describe_no_model_error,
            fabric_has_real_model)
        try:
            has_real = fabric_has_real_model(fabric)
        except Exception:
            has_real = True
        if not has_real and not force:
            return describe_no_model_error(fabric=fabric)
        if has_real and not force and not dry_run:
            # Static registration is not reachability: an Ollama model that
            # is registered but whose server is down would plan a whole
            # project and then fail its first coding task. The same bounded
            # live probe ProjectBuilder uses answers before we start.
            try:
                report = check_fabric_readiness(
                    fabric, probe_network=True, timeout=5.0)
            except Exception:
                report = None
            if report is not None and not report.ready:
                return describe_no_model_error(report)
        return None

    def _fabric_or_default(self) -> Any:
        if self.fabric is not None:
            return self.fabric
        from forge.models.fabric import ModelFabric

        return ModelFabric.from_defaults()

    # -- execution ---------------------------------------------------------

    def _execute_plan(self, store: PlanStore, plan: ProjectPlan, *,
                      on_event: Any, started: float,
                      resumed: bool) -> AutoReport:
        if plan.status == PlanStatus.APPROVED.value:
            try:
                plan = store.checkout_for_execution(plan.id)
            except PlanError as exc:
                return AutoReport(status=STATUS_REFUSED, plan_id=plan.id,
                                  requirement=plan.requirement.statement,
                                  reason=str(exc))
        self._emit(on_event, "execution_started", {
            "plan_id": plan.id, "resumed": resumed,
            "tasks": [task.id for task in plan.tasks]})

        git = self._git()
        start_commit = self._head_sha(git)
        state = self._load_state(plan.id)
        state.setdefault("requirement", plan.requirement.statement)
        state.setdefault("start_commit", start_commit)
        state.setdefault("started_at", time.time())
        # Every (re)start of the loop is a fresh pass: blocked tasks get a
        # full attempt budget again, while the attempt log keeps the whole
        # history for audit.
        if resumed:
            state["pass"] = int(state.get("pass", 1) or 1) + 1
        else:
            state.setdefault("pass", 1)

        order = {task.id: index
                 for index, task in enumerate(plan.tasks)}
        outcomes: Dict[str, TaskOutcome] = {
            item["task_id"]: TaskOutcome(
                task_id=str(item.get("task_id", "")),
                title=str(item.get("title", "")),
                role=str(item.get("role", "")),
                status=str(item.get("status", "")),
                attempts=int(item.get("attempts", 0) or 0),
                error=str(item.get("error", "")),
                files=tuple(item.get("files") or ()),
                evidence=dict(item.get("evidence") or {}))
            for item in state.get("outcomes", [])
            if isinstance(item, dict) and item.get("task_id")
        }

        guard = 0
        while guard < 1000:
            guard += 1
            ready = plan.ready_tasks()
            if not ready:
                break
            task = min(ready, key=lambda item: order.get(item.id, 1 << 30))
            outcome = self._execute_task(
                store, plan, task, git=git, state=state,
                start_commit=start_commit, on_event=on_event)
            outcomes[task.id] = outcome
            task.status = "done" if outcome.status == "done" else "blocked"
            store.save(plan)
            self._save_state(plan.id, state, outcomes)
            self._emit(on_event, "task_finished", {
                "task_id": task.id, "status": outcome.status,
                "attempts": outcome.attempts, "error": outcome.error[:400]})

        task_list = sorted(
            (outcomes.get(task.id) or TaskOutcome(
                task_id=task.id, title=task.title, role=task.role,
                status="skipped" if task.status == "pending" else task.status)
             for task in plan.tasks),
            key=lambda item: order.get(item.task_id, 1 << 30))
        blocked = [item for item in task_list if item.status != "done"]
        status = STATUS_COMPLETED if not blocked else STATUS_BLOCKED
        reason = ""
        if status == STATUS_COMPLETED:
            plan = store.complete(plan.id)
            self._emit(on_event, "plan_completed", {"plan_id": plan.id})
        else:
            reason = ("%d task(s) not done: %s" % (
                len(blocked),
                ", ".join("%s (%s)" % (item.task_id,
                                       item.error[:80] or item.status)
                          for item in blocked[:8])))
        return AutoReport(
            status=status, plan_id=plan.id,
            requirement=plan.requirement.statement, reason=reason,
            tasks=task_list,
            files_changed=self._files_since(git, start_commit),
            commits=self._commits_since(git, start_commit),
            start_commit=start_commit,
            duration_seconds=time.time() - started,
            resumed=resumed, plan_summary=plan.summary())

    def _execute_task(self, store: PlanStore, plan: ProjectPlan,
                      task: PlanTask, *, git: Any, state: Dict[str, Any],
                      start_commit: str, on_event: Any) -> TaskOutcome:
        outcome = TaskOutcome(task_id=task.id, title=task.title,
                              role=task.role)
        attempts_log: List[Dict[str, Any]] = list(
            (state.get("attempts") or {}).get(task.id) or [])
        current_pass = int(state.get("pass", 1) or 1)
        base_attempts = sum(1 for item in attempts_log
                            if int(item.get("pass", 0) or 0) == current_pass)
        max_attempts = 1 + self.max_task_retries
        failure_context = ""
        for item in reversed(attempts_log):
            if item.get("error"):
                failure_context = str(item["error"])
                break

        attempt = base_attempts
        while attempt < max_attempts:
            attempt += 1
            self._emit(on_event, "task_started", {
                "task_id": task.id, "role": task.role,
                "attempt": attempt, "max_attempts": max_attempts})
            try:
                ok, files, evidence, error = self._run_step(
                    plan, task, git=git, start_commit=start_commit,
                    failure_context=failure_context)
            except Exception as exc:  # a step bug must not kill the run
                ok, files, evidence, error = False, (), {}, str(exc)[:2000]
            attempts_log.append({
                "attempt": attempt, "pass": current_pass,
                "at": time.time(), "ok": bool(ok),
                "files": list(files)[:100],
                "error": (error or "")[:MAX_EVIDENCE_CHARS]})
            state.setdefault("attempts", {})[task.id] = attempts_log
            if ok:
                outcome.status = "done"
                outcome.attempts = attempt
                outcome.files = tuple(files)
                outcome.evidence = evidence
                return outcome
            failure_context = error
            self._emit(on_event, "task_failed", {
                "task_id": task.id, "attempt": attempt,
                "error": (error or "")[:400]})

        outcome.status = "blocked"
        outcome.attempts = attempt
        outcome.error = (failure_context or "task did not succeed")[:2000]
        return outcome

    # -- per-role steps ------------------------------------------------------

    def _run_step(self, plan: ProjectPlan, task: PlanTask, *, git: Any,
                  start_commit: str, failure_context: str
                  ) -> Tuple[bool, Tuple[str, ...], Dict[str, Any], str]:
        """Run one task the honest way for its role.

        Returns ``(ok, files_changed, evidence, error)``.
        """
        role = (task.role or "").strip().lower()
        if role == ARCHITECT_ROLE:
            return self._step_plan_evidence(plan, task)
        if role == RESEARCH_ROLE:
            return self._step_research(plan, task)
        if role == REVIEW_ROLE:
            return self._step_review_gate(plan, task, git, start_commit,
                                          failure_context)
        if role == SECURITY_ROLE:
            return self._step_security_gate(plan, task, git, start_commit,
                                            failure_context)
        if role == PERFORMANCE_ROLE:
            return self._step_benchmark_baseline(plan, task)
        if role == RELEASE_ROLE:
            return self._step_release_gate(plan, task, failure_context)
        # Every other role (coding, documentation, hardware, and anything
        # this map does not know) is expected to produce files, so it gets
        # the full guarded Supervisor transaction.
        return self._step_supervisor(plan, task, failure_context)

    def _step_supervisor(self, plan: ProjectPlan, task: PlanTask,
                         failure_context: str
                         ) -> Tuple[bool, Tuple[str, ...], Dict[str, Any], str]:
        """One full guarded Supervisor transaction for this task."""
        from forge.core.supervisor import Supervisor

        requirement = self._task_requirement(plan, task, failure_context)
        supervisor = Supervisor(self.project_id, root=self.root)
        result = supervisor.run(
            requirement,
            approved=True,
            fabric=self._fabric_or_default(),
            max_debug_retries=self.max_debug_retries,
            mode=self.mode,
        )
        files = tuple(str(path) for path in (result.get("files") or [])
                      if isinstance(path, str))
        evidence = {
            "accepted": bool(result.get("accepted")),
            "model": str(result.get("selected_model") or ""),
            "provider": str(result.get("selected_provider") or ""),
            "summary": str(result.get("summary") or "")[:500],
            "stages": list(result.get("stages") or []),
            "run_id": str(result.get("run_id") or ""),
        }
        if result.get("accepted"):
            return True, files, evidence, ""
        error = str(result.get("error") or "supervisor run was not accepted")
        evidence["failed_gates"] = list(
            (result.get("acceptance") or {}).get("failed_gates") or [])
        return False, files, evidence, error[:2000]

    def _step_plan_evidence(self, plan: ProjectPlan, task: PlanTask
                            ) -> Tuple[bool, Tuple[str, ...], Dict[str, Any], str]:
        """The architect task is satisfied by the plan itself — verifiable.

        The acceptance condition is that every specification has an id, an
        area, and a stated way of checking it. That is checked, not
        assumed.
        """
        problems = [
            "spec %s has no %s" % (item.id or "?", missing)
            for item in plan.specifications
            for missing in ("id", "area", "verifiable_by")
            if not str(getattr(item, missing, "") or "").strip()
        ]
        if not plan.specifications:
            problems.append("the plan has no specifications")
        evidence = {"specifications": len(plan.specifications),
                    "required": sum(1 for item in plan.specifications
                                    if item.required)}
        if problems:
            return False, (), evidence, "; ".join(problems[:8])
        return True, (), evidence, ""

    def _step_research(self, plan: ProjectPlan, task: PlanTask
                       ) -> Tuple[bool, Tuple[str, ...], Dict[str, Any], str]:
        """Evidence-bound research recorded in long-term project memory.

        The acceptance condition is that findings — or their honest absence —
        are recorded with sources. Both outcomes are recorded; nothing is
        guessed.
        """
        from forge.knowledge import ProjectMemory
        from forge.research.engine import ResearchEngine

        question = ("%s: %s" % (plan.requirement.kind,
                                plan.requirement.statement))[:1800]
        try:
            result = ResearchEngine(self.root).ask(question)
        except Exception as exc:
            return False, (), {}, "research engine failed: %s" % str(exc)[:500]
        evidence = {
            "confidence": result.get("confidence", 0.0),
            "evidence_items": len(result.get("evidence") or []),
            "answer": str(result.get("answer") or "")[:800],
        }
        try:
            memory = ProjectMemory(self.root)
            entry = memory.add(
                "design", "AutoPilot research for plan %s" % plan.id,
                str(result.get("answer") or "")[:1500],
                source="autopilot",
                payload={"question": question[:800],
                         "confidence": result.get("confidence", 0.0),
                         "evidence": (result.get("evidence") or [])[:20],
                         "task_id": task.id})
            evidence["memory_entry"] = entry.id
        except Exception as exc:
            return False, (), evidence, \
                "research could not be recorded: %s" % str(exc)[:400]
        return True, (), evidence, ""

    def _step_review_gate(self, plan: ProjectPlan, task: PlanTask, git: Any,
                          start_commit: str, failure_context: str
                          ) -> Tuple[bool, Tuple[str, ...], Dict[str, Any], str]:
        """Independent review over everything the run has changed so far.

        A failing verdict triggers one bounded repair through the
        Supervisor, then the review runs again. The gate's word is final.
        """
        return self._gate_with_repair(
            plan, task, failure_context,
            check=lambda: self._review_now(git, start_commit),
            repair_requirement=self._review_repair_requirement)

    def _review_now(self, git: Any, start_commit: str
                    ) -> Tuple[bool, Dict[str, Any], str]:
        from forge.security.review import ReviewGate

        diff, changed = self._diff_since(git, start_commit)
        decision = ReviewGate(str(self.root)).review(
            diff, changed, requirement="AutoPilot cumulative review")
        evidence = {
            "verdict": decision.verdict.value,
            "findings": len(decision.findings),
            "by_severity": {},
            "changed_files": len(changed),
        }
        for finding in decision.findings:
            severity = finding.severity.value \
                if hasattr(finding.severity, "value") else str(finding.severity)
            evidence["by_severity"][severity] = \
                evidence["by_severity"].get(severity, 0) + 1
        approved = decision.verdict.value == "APPROVE"
        detail = "" if approved else \
            "; ".join(str(item.message)[:120]
                      for item in decision.findings[:6])
        return approved, evidence, detail

    def _step_security_gate(self, plan: ProjectPlan, task: PlanTask,
                            git: Any, start_commit: str,
                            failure_context: str
                            ) -> Tuple[bool, Tuple[str, ...], Dict[str, Any], str]:
        """Security verification over everything the run has changed."""
        return self._gate_with_repair(
            plan, task, failure_context,
            check=lambda: self._security_now(git, start_commit),
            repair_requirement=self._security_repair_requirement)

    def _security_now(self, git: Any, start_commit: str
                      ) -> Tuple[bool, Dict[str, Any], str]:
        from forge.security.verification import VerificationPipeline

        _diff, changed = self._diff_since(git, start_commit)
        result = VerificationPipeline(self.root).security(changed)
        findings = list((result.evidence or {}).get("findings") or [])
        evidence = {
            "passed": bool(result.passed),
            "findings": len(findings),
            "detail": str(result.details)[:500],
        }
        detail = "" if result.passed else \
            "; ".join(str(item.get("path", "?")) + ": "
                      + str(item.get("reason", ""))[:80]
                      for item in findings[:6] if isinstance(item, dict))
        return bool(result.passed), evidence, detail

    def _step_release_gate(self, plan: ProjectPlan, task: PlanTask,
                           failure_context: str
                           ) -> Tuple[bool, Tuple[str, ...], Dict[str, Any], str]:
        """Release readiness: the repo's own tests and build must pass."""

        def check() -> Tuple[bool, Dict[str, Any], str]:
            from forge.security.verification import VerificationPipeline

            pipeline = VerificationPipeline(self.root)
            tests = pipeline.tests()
            build = pipeline.build()
            evidence = {
                "tests_passed": bool(tests.passed),
                "build_passed": bool(build.passed),
                "tests_detail": str(tests.details)[:300],
                "build_detail": str(build.details)[:300],
            }
            ok = bool(tests.passed and build.passed)
            detail = "" if ok else "failing gates: " + ", ".join(
                gate.name for gate in (tests, build) if not gate.passed)
            return ok, evidence, detail

        def repair(detail: str) -> str:
            return self._assemble_repair_requirement(
                plan, task,
                "The release gate failed its own tests/build. %s" % detail)

        return self._gate_with_repair(plan, task, failure_context,
                                      check=check, repair_requirement=repair)

    def _step_benchmark_baseline(self, plan: ProjectPlan, task: PlanTask
                                 ) -> Tuple[bool, Tuple[str, ...], Dict[str, Any], str]:
        """Measure a real baseline; a baseline is runs, not a claim."""
        from forge.self_development.benchmark import BenchmarkRunner

        runs = max(1, min(MAX_BENCHMARK_RUNS,
                          int(plan.benchmark_plan.min_runs or 1)))
        runner = BenchmarkRunner(self.root)
        results = []
        for _ in range(runs):
            results.append(runner.run_benchmarks())
        last = results[-1]
        passed = all(item.passed_benchmarks >= item.total_benchmarks
                     for item in results)
        evidence = {
            "runs": len(results),
            "min_runs_required": int(plan.benchmark_plan.min_runs or 1),
            "passed": bool(passed),
            "last_total": int(last.total_benchmarks),
            "last_passed": int(last.passed_benchmarks),
            "mean_duration": round(sum(item.duration for item in results)
                                   / max(1, len(results)), 3),
            "details_keys": sorted(str(key)
                                   for key in (last.details or {}).keys()),
        }
        if not passed:
            return False, (), evidence, \
                "benchmark baseline shows failing measurements"
        return True, (), evidence, ""

    # -- helpers -------------------------------------------------------------

    def _gate_with_repair(self, plan: ProjectPlan, task: PlanTask,
                          failure_context: str, *,
                          check: Callable[[], Tuple[bool, Dict[str, Any], str]],
                          repair_requirement: Callable[[str], str],
                          ) -> Tuple[bool, Tuple[str, ...], Dict[str, Any], str]:
        """Check a deterministic gate; on failure run one bounded repair.

        The repair is a real Supervisor transaction driven by the gate's
        findings; the gate itself is then re-run and its verdict is final —
        never this method's opinion. Every attempt carries a real repair
        (fed any previous failure), so retries are never wasted.
        """
        ok, evidence, detail = check()
        if ok:
            return True, (), evidence, ""
        requirement = repair_requirement(detail)
        if failure_context:
            requirement = ("%s\n\nPREVIOUS REPAIR ATTEMPT FAILED:\n%s"
                           % (requirement,
                              failure_context[:MAX_FAILURE_CONTEXT_CHARS])
                           )[:MAX_TASK_REQUIREMENT_CHARS]
        repair_files = self._repair_through_supervisor(plan, task,
                                                       requirement)
        ok, evidence2, detail = check()
        evidence = {**evidence, "after_repair": evidence2}
        if ok:
            return True, repair_files, evidence, ""
        return False, repair_files, evidence, detail or "gate failed"

    def _repair_through_supervisor(self, plan: ProjectPlan, task: PlanTask,
                                   requirement: str) -> Tuple[str, ...]:
        from forge.core.supervisor import Supervisor

        result = Supervisor(self.project_id, root=self.root).run(
            requirement[:MAX_TASK_REQUIREMENT_CHARS],
            approved=True, fabric=self._fabric_or_default(),
            max_debug_retries=self.max_debug_retries, mode=self.mode)
        return tuple(str(path) for path in (result.get("files") or [])
                     if isinstance(path, str))

    def _review_repair_requirement(self, detail: str) -> str:
        return ("An independent code review over the project's changes "
                "raised findings that block approval. Fix the code so the "
                "review findings are resolved. Findings: %s"
                % (detail or "see the repository diff"))[:MAX_TASK_REQUIREMENT_CHARS]

    def _security_repair_requirement(self, detail: str) -> str:
        return ("The security verification gate found problems in this "
                "project's files. Remove the secrets/dangerous constructs "
                "listed here without weakening functionality. Findings: %s"
                % (detail or "see the security report"))[:MAX_TASK_REQUIREMENT_CHARS]

    def _assemble_repair_requirement(self, plan: ProjectPlan, task: PlanTask,
                                     detail: str) -> str:
        return ("PROJECT: %s\n\nTASK %s (%s): %s\n\n%s\n\nProduce concrete "
                "file changes that fix this. All tests must pass." % (
                    plan.requirement.statement[:2000], task.id, task.role,
                    task.title, detail))[:MAX_TASK_REQUIREMENT_CHARS]

    def _task_requirement(self, plan: ProjectPlan, task: PlanTask,
                          failure_context: str) -> str:
        """Assemble the Supervisor requirement for one plan task.

        The first line names the task: the Supervisor commits with
        ``forge: <requirement>``, so this is also what lands in the
        repository's history — one readable commit per plan task.
        """
        parts: List[str] = []
        parts.append("TASK %s (%s): %s — implement this task of an "
                     "approved project plan." % (task.id, task.role,
                                                 task.title))
        parts.append("")
        parts.append("PROJECT REQUIREMENT:")
        parts.append(plan.requirement.statement[:3000])
        parts.append("")
        parts.append("TASK %s (%s): %s" % (task.id, task.role, task.title))
        if task.acceptance:
            parts.append("Acceptance for this task: %s" % task.acceptance)
        component = plan.component(task.component) if task.component else None
        if component is not None:
            interfaces = ", ".join(component.interfaces) or "none declared"
            parts.append("Component %r: %s (interfaces: %s)" % (
                component.name, component.responsibility, interfaces))
        done = [item for item in plan.tasks if item.status == "done"]
        if done:
            parts.append("")
            parts.append("Already completed in this project:")
            for item in done[:12]:
                parts.append("- %s %s" % (item.id, item.title))
        specs = [item for item in plan.specifications if item.required]
        if specs:
            parts.append("")
            parts.append("Project specifications:")
            for item in specs[:12]:
                parts.append("- [%s] %s" % (item.id, item.statement))
        if failure_context:
            parts.append("")
            parts.append("PREVIOUS ATTEMPT FAILED and was rolled back:")
            parts.append(failure_context[:MAX_FAILURE_CONTEXT_CHARS])
            parts.append("Diagnose that failure and produce a corrected "
                         "change set.")
        text = "\n".join(parts)
        return text[:MAX_TASK_REQUIREMENT_CHARS]

    # -- git helpers ---------------------------------------------------------

    def _git(self) -> Any:
        from forge.tools.git import GitTool

        return GitTool(self.root)

    def _head_sha(self, git: Any) -> str:
        result = git.run("rev-parse", "HEAD")
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return EMPTY_TREE_SHA

    def _diff_since(self, git: Any, start_commit: str
                    ) -> Tuple[str, Tuple[str, ...]]:
        diff = git.run("diff", start_commit, "HEAD")
        names = git.run("diff", "--name-only", start_commit, "HEAD")
        changed = tuple(line.strip() for line in names.stdout.splitlines()
                        if line.strip())
        return diff.stdout, changed

    def _files_since(self, git: Any, start_commit: str) -> Tuple[str, ...]:
        _diff, changed = self._diff_since(git, start_commit)
        return changed

    def _commits_since(self, git: Any, start_commit: str) -> int:
        if start_commit == EMPTY_TREE_SHA:
            result = git.run("rev-list", "--count", "HEAD")
        else:
            result = git.run("rev-list", "--count",
                             "%s..HEAD" % start_commit)
        if result.returncode != 0:
            return 0
        try:
            return int(result.stdout.strip() or "0")
        except ValueError:
            return 0

    # -- state persistence -----------------------------------------------------

    def _state_path(self, plan_id: str) -> Path:
        safe = "".join(char for char in (plan_id or "")
                       if char.isalnum() or char in "-_")
        return self.root / AUTOPILOT_DIR / ("run-%s.json" % (safe or "plan"))

    def _load_state(self, plan_id: str) -> Dict[str, Any]:
        path = self._state_path(plan_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_state(self, plan_id: str, state: Dict[str, Any],
                    outcomes: Dict[str, TaskOutcome]) -> None:
        payload = dict(state)
        payload["plan_id"] = plan_id
        payload["updated_at"] = time.time()
        payload["outcomes"] = [outcome.to_dict()
                               for outcome in outcomes.values()]
        try:
            path = self._state_path(plan_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2, sort_keys=True,
                                       default=str) + "\n",
                            encoding="utf-8")
        except OSError:
            # Losing bookkeeping never loses work: the plan file itself
            # already carries task statuses, so execution stays resumable.
            pass

    # -- events ---------------------------------------------------------------

    @staticmethod
    def _emit(on_event: Any, name: str, details: Dict[str, Any]) -> None:
        if on_event is None:
            return
        try:
            on_event(name, dict(details))
        except Exception:
            pass
