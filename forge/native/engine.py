"""The Forge Native AI Engine: one task → understand → plan → act → verify.

``NativeAIEngine`` is the central first-party orchestrator (A81 layer 1). It
owns the *lifecycle*, not new low-level machinery:

* task understanding + planning → :mod:`forge.native.planner` (+ the
  reasoning hub for neural enrichment);
* repository inspection → ``RepositoryIntelligence`` (existing layer);
* context construction → :mod:`forge.native.context`;
* action planning → the plan's own step list drives every stage;
* tool execution / applying authorized edits → :mod:`forge.native.coding`
  (ChangeSet engine + policy gate + permissioned runtime — the engine has
  no other way to touch files);
* verification → :mod:`forge.native.verification`;
* debugging → :mod:`forge.native.debugging` (bounded);
* memory → :mod:`forge.native.memory`;
* final reporting → :mod:`forge.native.reporting`.

Status is mirrored to ``.forge/native/state.json`` for the desktop panel and
CLI (see :mod:`forge.native.state`).

Security posture (enforced by construction, not by convention):

* the engine never self-authorizes: writes need the caller's ``approved``
  flag or an A33 approval token, and DENY is never escalated (the policy
  gate owns those semantics). ``OperationMode.SAFE`` even blocks test
  execution — the engine then reports tests as ``not_executed`` instead of
  sneaking around the mode;
* it cannot escape the workspace: every read/write goes through the rooted
  filesystem tools;
* it never disables policy, never exposes credentials (reports are
  redacted), and — critically — **never reports unperformed work as
  completed**: an edit-class plan without a neural backend finishes as
  ``NEEDS_MODEL`` with the refused steps listed, and any applied work that
  later fails tests or verification is rolled back exactly (checkpoint
  restore).

The engine composes — it does not replace — the existing stack: the
Supervisor, Model Fabric, and A32/A33 layers stay authoritative for their
own surfaces; ``forge run --native`` routes a task through this engine
instead of directly through the Supervisor.

Default mode is ``ASSISTED`` (same posture as the A32 supervisor):
read-only work and constrained test execution run freely, every write needs
the caller's approval or an A33 token. ``SAFE`` is stricter — even test
execution is blocked — and the engine reports that as ``not_executed``
rather than routing around the mode.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Dict, List, Optional, Tuple

from forge.native.capabilities import capability_matrix
from forge.native.coding import NativeCodingEngine
from forge.native.context import NativeContextEngine
from forge.native.debugging import NativeDebugLoop
from forge.native.memory import NativeMemory
from forge.native.planner import NativePlanner, StepKind
from forge.native.reasoning import (
    NativeDeterministicBackend,
    ReasoningHub,
    ReasoningKind,
    ReasoningRequest,
    RefusalCode,
)
from forge.native.reporting import NativeRunReport, new_run_id
from forge.native.state import (
    EngineState,
    StageKind,
    StateTracker,
    TaskState,
    VerificationStatus,
)
from forge.native.verification import NativeVerifier
from forge.security.permissions import OperationMode

#: Where run records live (consumed by the CLI, panel, and dataset builder).
RUNS_RELATIVE_DIR = Path(".forge") / "native" / "runs"


class _Cancelled(Exception):
    """Internal control flow for cooperative cancellation."""


class NativeRunResult:
    """The caller-facing outcome of :meth:`NativeAIEngine.run`."""

    def __init__(self, report: NativeRunReport,
                 snapshot: Dict[str, Any]) -> None:
        self.report = report
        self.snapshot = snapshot

    @property
    def final_status(self) -> str:
        return self.report.final_status

    @property
    def ok(self) -> bool:
        return self.report.final_status == "COMPLETED"

    @property
    def needs_model(self) -> bool:
        return self.report.final_status == "NEEDS_MODEL"

    @property
    def files_changed(self) -> List[str]:
        return list(self.report.files_changed)

    def to_dict(self) -> Dict[str, Any]:
        return {"report": self.report.to_dict(),
                "snapshot": dict(self.snapshot)}


class NativeAIEngine:
    """First-party engineering engine for low-power Forge installations."""

    def __init__(self, root: str | Path = ".", project: str = "forge-ai",
                 mode: Any = OperationMode.ASSISTED,
                 fabric: Any = None,
                 context_max_tokens: int = 1600,
                 max_debug_retries: int = 3,
                 memory_enabled: bool = True,
                 persist_status: bool = True,
                 dataset_capture: bool = False,
                 policy: Any = None,
                 approval_store: Any = None,
                 audit_log: Any = None,
                 model_data_policy: Any = None,
                 on_event: Optional[Callable[[str, Dict[str, Any]],
                                            None]] = None) -> None:
        self.root = Path(root).resolve()
        self.project = project
        self.mode = OperationMode(mode)
        self.fabric = fabric
        self.dataset_capture = bool(dataset_capture)
        self.on_event = on_event
        self._cancel = threading.Event()

        # -- permissions first: every write path below is built on this chain.
        from forge.runtime.defaults import create_default_runtime
        from forge.security.permissions import PermissionManager
        from forge.security.policy_gate import PolicyGate
        self.permissions = PermissionManager(mode=self.mode, policy=policy,
                                             store=approval_store,
                                             agent="forge-native-ai",
                                             audit=audit_log)
        self.policy_gate = PolicyGate(self.permissions)
        self.runtime = create_default_runtime(self.permissions,
                                              str(self.root))

        # -- reasoning hub: deterministic always; neural only via the fabric --
        backends: List[Any] = [NativeDeterministicBackend()]
        if fabric is not None:
            from forge.native.reasoning import (
                LocalNeuralBackend,
                RemoteNeuralBackend,
            )
            backends.append(LocalNeuralBackend(
                fabric, model_data_policy=model_data_policy))
            backends.append(RemoteNeuralBackend(
                fabric, model_data_policy=model_data_policy))
        self.hub = ReasoningHub(backends)

        # -- layers -------------------------------------------------------------
        self.planner = NativePlanner()
        self.context_engine = NativeContextEngine(
            max_tokens=context_max_tokens)
        self.memory = NativeMemory(self.root, enabled=memory_enabled)
        self.coding = NativeCodingEngine(self.root, self.runtime,
                                         hub=self.hub)
        self.verifier = NativeVerifier(self.root)
        self.debug_loop = NativeDebugLoop(self.coding, self.hub,
                                          max_retries=max_debug_retries,
                                          root=self.root)
        self.tracker = StateTracker(self.root, persist=persist_status,
                                    project=project)
        self._intelligence: Any = None
        self._git_checked: Optional[bool] = None
        self._run_started = perf_counter()

    # -- status / control -------------------------------------------------------

    def cancel(self) -> None:
        """Cooperative cancellation: checked at every stage boundary.

        Sticky on purpose: a cancel raised *before* ``run`` aborts that run
        at its first boundary (callers decide when cancellation ends), and
        :meth:`resume` clears it explicitly.
        """
        self._cancel.set()

    def resume(self) -> None:
        """Clear a previous cancellation so the engine accepts the next run."""
        self._cancel.clear()

    def run_probe_plan(self, task: str,
                       full: bool = False) -> Dict[str, Any]:
        """Cheap plan probe for CLI pre-flight guidance (no execution).

        Only classifies and plans — touches no files, calls no tools, runs
        no tests — so a caller can decide whether approval is needed before
        committing to a full run.
        """
        try:
            intelligence = self._ensure_intelligence()
        except Exception:
            intelligence = None
        try:
            plan = NativePlanner(intelligence).plan(task, intelligence)
            out = {"ok": True, "task_class": plan.task_class,
                   "has_edit_step": plan.has_edit_step(),
                   "steps": len(plan.steps),
                   "confidence": plan.confidence}
            if full:
                out["plan"] = plan.to_dict()
            return out
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def status(self) -> Dict[str, Any]:
        hub_status = self.hub.status()
        model_state = self._model_backend_state()
        self.tracker.set_backends(
            self._reasoning_backend_payload(hub_status), model_state)
        return {
            "engine": "forge-native-ai",
            "engine_version": 1,
            "project": self.project,
            "root": str(self.root),
            "mode": self.mode.value,
            "state": self.tracker.as_dict(),
            "reasoning": hub_status,
            "model_backend": model_state,
            "capabilities": [c.to_dict() for c in capability_matrix()],
            "memory": self.memory.summary(),
        }

    def _reasoning_backend_payload(self, hub_status: Dict[str, Any]
                                   ) -> Dict[str, Any]:
        active = hub_status.get("active_backend") or {}
        generative = hub_status.get("generative_backend") or {}
        return {
            "name": active.get("name", "none"),
            "neural": bool(active.get("neural", False)),
            "role": "structural",
            "generative_backend": generative.get("name") or None,
            "generative_ready": bool(
                hub_status.get("generative_ready", False)),
        }

    def _model_backend_state(self) -> Dict[str, Any]:
        if self.fabric is None:
            return {"configured": False,
                    "detail": "no Model Fabric attached — analysis, "
                              "planning, testing, verification, and memory "
                              "run; code/repair generation is refused"}
        from forge.models.readiness import fabric_has_real_model
        try:
            real = bool(fabric_has_real_model(self.fabric))
        except Exception as exc:
            return {"configured": True, "real_model": False,
                    "detail": "fabric inspection failed: %s" % exc}
        names: List[str] = []
        if real:
            try:
                names = [str(model.name)
                         for model in self.fabric.registry.list()
                         if getattr(model, "provider", "") != "local"]
            except Exception:
                names = []
        # Honest liveness: the declarative ``available`` flag defaults to
        # True, so it proves nothing. Only health transitions from *real
        # calls* (healthy/degraded/unhealthy — never "unknown") count as
        # evidence a model has actually answered.
        verified = "unverified (registration only; no completed calls yet)"
        try:
            health = self.fabric.health()
            for name, state in health.items():
                if name == "local-fallback":
                    continue
                if str(state.get("health", "unknown")) in (
                        "healthy", "degraded", "unhealthy"):
                    verified = "evidence_from_calls: %s=%s" % (
                        name, state.get("health"))
                    break
        except Exception:
            pass
        return {"configured": True, "real_model": real,
                "models": names[:10], "live": verified,
                "detail": ("real model(s) registered: %s (registration "
                           "only — reachability is learned from actual "
                           "calls, never asserted)"
                           % ", ".join(names[:5]) if real else
                           "only the deterministic fallback placeholder is "
                           "registered; it cannot generate code "
                           "(NEURAL_REQUIRED refusals are expected)")}

    # -- helpers ------------------------------------------------------------------

    def _ensure_intelligence(self) -> Any:
        if self._intelligence is None:
            from forge.intelligence.repository import RepositoryIntelligence
            self._intelligence = RepositoryIntelligence.build(self.root)
        return self._intelligence

    def _event(self, report: NativeRunReport, name: str,
               details: Optional[Dict[str, Any]] = None) -> None:
        payload = details or {}
        report.record_event(name, perf_counter() - self._run_started,
                            payload)
        if self.on_event is not None:
            try:
                self.on_event(name, dict(payload))
            except Exception:
                # Live observability must never break the run.
                pass

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise _Cancelled()

    # -- the pipeline -------------------------------------------------------------

    def run(self, task: str, approved: bool = False,
            approval_token_id: str = "") -> NativeRunResult:
        """Execute one engineering task end to end (see module docstring)."""
        if not task or not str(task).strip():
            raise ValueError("the native engine cannot run an empty task")
        task = " ".join(str(task).split())
        run_id = new_run_id()
        report = NativeRunReport(run_id=run_id,
                                 task_id="native-%s" % run_id,
                                 project=self.project, requirement=task,
                                 started_at=time.time())
        self.tracker.set_run_id(run_id)
        self.tracker.start_task(report.task_id, task)
        self.tracker.set_retry(0, self.debug_loop.max_retries, False)
        self._run_started = perf_counter()
        applied_files: List[str] = []
        review_failed = False
        verification = None
        needs_model = False

        try:
            # 1. understand ------------------------------------------------------
            self._check_cancel()
            self._set_stage(report, EngineState.UNDERSTANDING)
            understanding = self.hub.respond(ReasoningRequest(
                kind=ReasoningKind.UNDERSTAND, task=task))
            classification = (understanding.data.get("classification")
                              if understanding.ok else None)

            # 2. inspect: repository intelligence ---------------------------------
            self._check_cancel()
            self._set_stage(report, EngineState.INSPECTING)
            try:
                intelligence = self._ensure_intelligence()
                report.executed_without_model.append("repository_analysis")
            except Exception as exc:
                report.notes.append("repository intelligence unavailable: %s"
                                    % exc)
                intelligence = None

            # 3. plan ---------------------------------------------------------------
            self._check_cancel()
            self._set_stage(report, EngineState.PLANNING)
            plan = NativePlanner(intelligence).plan(task, intelligence)
            report.plan = plan.to_dict()
            report.task_class = plan.task_class
            if classification:
                report.plan["classification_signals"] = classification
            self._event(report, "planned", {
                "task_class": plan.task_class, "steps": len(plan.steps),
                "confidence": plan.confidence})

            # 4. context ----------------------------------------------------------
            self._check_cancel()
            self._set_stage(report, EngineState.CONTEXT)
            context = self.context_engine.build(
                task, plan.to_dict(), intelligence, self.memory,
                git_root=self.root,
                target_files=plan.matched_files,
                target_symbols=plan.matched_symbols)
            report.context = context.to_dict()
            report.files_read = [entry["path"] for entry in context.items
                                 ][:40] or list(context.files)[:40]
            self._event(report, "context_built", {
                "fingerprint": context.fingerprint,
                "tokens": context.estimated_tokens})

            # 5. reasoning enrichment (deterministic; neural suggestions labeled)
            self._check_cancel()
            self._set_stage(report, EngineState.REASONING)
            targets = self.hub.respond(ReasoningRequest(
                kind=ReasoningKind.SELECT_TARGETS, task=task,
                payload={"candidates": [dict(entry)
                                        for entry in context.items]}))
            if targets.ok and targets.data.get("targets"):
                report.plan["selected_targets"] = targets.data["targets"]
            self._update_backend_state(report)

            # 6. execute the plan's steps in dependency order -------------------
            neural_ready = bool(
                self.hub.status().get("generative_ready", False))
            test_run: Optional[Dict[str, Any]] = None
            tests_ok: Optional[bool] = None
            debug_done = False
            total = len(plan.steps)
            blocked = False
            for index, step in enumerate(plan.steps, start=1):
                self._check_cancel()
                self.tracker.set_stage(StageKind(step.kind), index, total)
                if step.kind == StepKind.INSPECT:
                    step.status = "done"
                    if intelligence is not None:
                        step.result = {
                            "source_files": len(
                                intelligence.architecture.source_files),
                            "packages": len(intelligence.architecture.packages),
                        }
                elif step.kind == StepKind.REASON:
                    step.status = "done"
                    step.result = {"task_class": plan.task_class,
                                   "confidence": plan.confidence,
                                   "method": ("deterministic + "
                                              "repository evidence")}
                elif step.kind == StepKind.EDIT:
                    status, files = self._execute_edit(
                        step, task, context.render(), report,
                        approved=approved,
                        approval_token_id=approval_token_id,
                        neural_ready=neural_ready)
                    if status == "needs_model":
                        needs_model = True
                        report.skipped_neural.append("code_generation")
                    elif status == "blocked":
                        blocked = True
                    elif status == "failed":
                        raise RuntimeError(report.error
                                           or "change pipeline failed")
                    else:
                        applied_files.extend(files)
                elif step.kind == StepKind.TEST:
                    run = self.coding.run_tests(step.test_targets or None,
                                                 task_id=report.task_id)
                    test_run = run.to_dict()
                    report.test_runs.append(test_run)
                    tests_ok = run.passed if run.executed else False
                    step.status = "done" if run.executed else "failed"
                    step.result = {"passed": run.passed,
                                   "exit_code": run.exit_code,
                                   "executed": run.executed,
                                   "scope": ("targeted"
                                             if step.test_targets
                                             else "full suite")}
                    self._event(report, "test_executed", {
                        "passed": run.passed, "exit_code": run.exit_code,
                        "output_chars": len(run.output)})
                elif step.kind == StepKind.DEBUG:
                    if test_run is None or test_run.get("passed"):
                        step.status = "done"
                        step.result = {"note": "no failing test run to "
                                               "debug"}
                        continue
                    self.tracker.set_state(EngineState.DEBUGGING)
                    debug_result = self.debug_loop.run(
                        task, context.render(),
                        test_paths=step.test_targets or None,
                        approved=approved, task_id=report.task_id)
                    debug_done = True
                    report.debug = debug_result.to_dict()
                    for rel in list(getattr(debug_result, "changed_files",
                                            ()) or []):
                        if rel not in applied_files:
                            applied_files.append(rel)
                            report.files_changed.append(rel)
                            self.tracker.record_files_changed([rel])
                    report.retries = len(debug_result.cycles)
                    self.tracker.set_retry(
                        len(debug_result.cycles),
                        self.debug_loop.max_retries,
                        active=not debug_result.success,
                        last_reason=debug_result.stopped_reason)
                    self._event(report, "debug_cycle", {
                        "cycles": len(debug_result.cycles),
                        "success": debug_result.success,
                        "stopped": debug_result.stopped_reason})
                    if debug_result.success:
                        tests_ok = True
                        step.status = "done"
                    elif debug_result.stopped_reason == "neural_required":
                        step.status = "skipped_no_model"
                        needs_model = True
                        report.skipped_neural.append("repair_generation")
                    else:
                        step.status = "failed"
                elif step.kind == StepKind.REVIEW:
                    if self._execute_review(step, task, report,
                                            applied_files):
                        review_failed = True
                elif step.kind == StepKind.FINISH:
                    step.status = "done"
            if debug_done and tests_ok and report.plan:
                # The debug loop's final passing retest supersedes the first
                # failing run: record that fact on the TEST step honestly.
                self._event(report, "tests_recovered",
                            {"after_cycles": report.retries})

            # refresh the report's plan so executed step statuses are real
            refreshed = plan.to_dict()
            for key in ("classification_signals", "selected_targets",
                        "edit_refusal"):
                if key in report.plan:
                    refreshed[key] = report.plan[key]
            report.plan = refreshed

            # 7. verification -----------------------------------------------------
            self._check_cancel()
            self._set_stage(report, EngineState.VERIFYING)
            # With edits applied, re-run the FULL suite here: the TEST step
            # may have been targeted, and scoped-off regression checks must
            # not slip through (same acceptance semantics as the A32 gate).
            # Without edits the real pytest already ran in the TEST step —
            # a second identical run would just burn the tiny host's time.
            verification = self.verifier.verify(
                changed_files=sorted(set(applied_files)) if applied_files
                else None,
                diff_text=self._diff_text(),
                run_tests=bool(applied_files),
                diff_material_available=self._is_git_worktree())
            report.verification = verification.to_dict()
            report.executed_without_model.extend(
                ["deterministic_orchestration", "test_execution",
                 "verification", "failure_classification"])
            self.tracker.set_verification(VerificationStatus(
                status=verification.status,
                executed=len(verification.executed),
                passed=sum(1 for gate in verification.executed
                           if gate.passed),
                failed=list(verification.failed),
                skipped=list(verification.skipped)))
            security_gate = next((gate for gate in verification.gates
                                  if gate.name == "security"), None)
            if security_gate is not None:
                report.security = security_gate.to_dict()
            self._event(report, "verified", {
                "status": verification.status,
                "failed": verification.failed})

            # 8. memorize ---------------------------------------------------------
            self._check_cancel()
            self._set_stage(report, EngineState.MEMORIZING)
            self._memorize(task, plan, verification, tests_ok,
                           debug_done, needs_model, report)

            # 9. final status -----------------------------------------------------
            self._check_cancel()
            failed_gates = list(verification.failed)
            if blocked:
                final, task_state = "BLOCKED_APPROVAL", TaskState.BLOCKED
            elif (applied_files and (tests_ok is False
                                     or review_failed
                                     or "tests" in failed_gates
                                     or "security" in failed_gates
                                     or "compile" in failed_gates
                                     or "diff_validation" in failed_gates)):
                self._rollback(sorted(set(applied_files)), report)
                if review_failed and tests_ok is not False and not (
                        {"tests", "security", "compile",
                         "diff_validation"} & set(failed_gates)):
                    report.error = ("independent review gate reported "
                                    "blocking findings; applied changes "
                                    "were rolled back")
                final, task_state = "FAILED", TaskState.FAILED
            elif needs_model:
                final, task_state = "NEEDS_MODEL", TaskState.NEEDS_MODEL
            elif review_failed:
                # Review-only task class: the blocking finding is the
                # deliverable itself. Surfaced as PARTIAL, never as success.
                final, task_state = "COMPLETED", TaskState.PARTIAL
            elif verification.status == "PARTIAL":
                final, task_state = "COMPLETED", TaskState.PARTIAL
            else:
                final, task_state = "COMPLETED", TaskState.SUCCEEDED
            report.final_status = final
            report.duration_seconds = perf_counter() - self._run_started
            self.coding.release_checkpoint()
            self.tracker.set_state(EngineState[final])
            self.tracker.end_task(task_state)
            self._event(report, "completed", {"status": final})
        except _Cancelled:
            self._finish_abnormal(report, "CANCELLED", TaskState.CANCELLED,
                                  EngineState.CANCELLED,
                                  "cancelled by operator", applied_files)
        except Exception as exc:
            self._finish_abnormal(report, "FAILED", TaskState.FAILED,
                                  EngineState.FAILED, str(exc),
                                  applied_files)
        finally:
            self._update_backend_state(report)
            report.dataset_captured = bool(self.dataset_capture)
            self._persist_run_record(report)
        return NativeRunResult(report, self.tracker.as_dict())

    # -- step executors --------------------------------------------------------------

    def _execute_edit(self, step: Any, task: str, context_text: str,
                      report: NativeRunReport, *, approved: bool,
                      approval_token_id: str,
                      neural_ready: bool) -> Tuple[str, List[str]]:
        """propose → validate → apply (all gated). Returns (status, files)."""
        if not neural_ready:
            # Prove the refusal is real: ask the hub, get a structured
            # refusal, and record it. Nothing is generated, nothing written.
            refusal = self.hub.respond(ReasoningRequest(
                kind=ReasoningKind.GENERATE, task=task,
                context=context_text))
            report.plan["edit_refusal"] = {
                "refusal": refusal.refusal,
                "message": refusal.message[:400],
                "honest_note": ("no model was called and no code was "
                                "generated; nothing was written"),
            }
            step.status = "skipped_no_model"
            step.result = {"skipped": RefusalCode.NEURAL_REQUIRED.value}
            self._event(report, "edit_skipped",
                        {"reason": RefusalCode.NEURAL_REQUIRED.value})
            return "needs_model", []
        self.coding.open_checkpoint()
        proposal = self.coding.propose_changes(task, context_text)
        step.result = {"proposal": proposal.to_dict()}
        if not proposal.ok:
            if proposal.refusal == RefusalCode.NEURAL_REQUIRED.value:
                step.status = "skipped_no_model"
                return "needs_model", []
            report.error = (proposal.message[:400]
                            or "model proposal failed")
            step.status = "failed"
            self._event(report, "proposal_failed",
                        {"refusal": proposal.refusal})
            return "failed", []
        validation = self.coding.validate_changes(proposal.changes,
                                                   approved=approved)
        step.result["validation"] = validation.to_dict()
        if not validation.ok:
            report.error = ("change proposal rejected by the ChangeSet "
                            "engine: %s"
                            % (validation.issues[0].reason
                               if validation.issues else "invalid"))
            step.status = "failed"
            self._event(report, "validation_failed",
                        {"issues": [issue.to_dict() for issue
                                    in validation.issues[:5]]})
            return "failed", []
        outcome = self.coding.apply_changes(
            proposal.changes, approved=approved, task_id=report.task_id,
            approval_token_id=approval_token_id,
            label="native-ai-edit")
        step.result["apply"] = outcome.to_dict()
        step.result["model"] = {"model": proposal.model,
                                "provider": proposal.provider,
                                "latency_ms": proposal.latency_ms,
                                "generated_by": proposal.generated_by}
        for decision in outcome.decisions:
            self._event(report, "permission_decision", decision)
        if not outcome.ok:
            if outcome.blocked_by_approval:
                report.error = ("write blocked by the policy gate: %s"
                                % (outcome.errors[0] if outcome.errors
                                   else "approval required"))
                step.status = "skipped_no_write_authorization"
                self._event(report, "change_blocked",
                            {"files": sorted(proposal.changes)})
                return "blocked", []
            report.error = (outcome.errors[0] if outcome.errors
                            else "apply failed")
            step.status = "failed"
            self._event(report, "apply_failed",
                        {"errors": outcome.errors[:5]})
            return "failed", []
        report.files_changed.extend(outcome.files)
        self.tracker.record_files_changed(outcome.files)
        if self.dataset_capture and proposal.raw:
            # Opt-in training-record capture: what the model actually said
            # for the proposal that was applied (redacted on serialization).
            report.model_output = {
                "text": proposal.raw[:8000],
                "model": proposal.model,
                "provider": proposal.provider,
                "task": report.requirement[:400],
            }
        self._event(report, "change_applied", {
            "files": list(outcome.files), "model": proposal.model,
            "checkpoint_id": outcome.checkpoint_id})
        step.status = "done"
        return "applied", list(outcome.files)

    def _execute_review(self, step: Any, task: str,
                        report: NativeRunReport,
                        applied_files: List[str]) -> bool:
        """Independent review over the actual diff; returns True on failure.

        The deterministic gate is A32's own ``ReviewGate`` -- the same
        conflict-marker / dynamic-exec / test-weakening severity rules the
        supervised pipeline enforces -- plus the verification pipeline's
        diff review. Model commentary (only when a real model is attached)
        is attached as findings and can never override or soften the gate.
        """
        changed = sorted(set(applied_files))
        if not changed:
            step.status = "done"
            step.result = {"note": "no changes to review"}
            return False
        diff_text = self._diff_text()
        review_failed = False
        try:
            from forge.security.review import ReviewGate
            decision = ReviewGate(self.root).review(diff_text, changed,
                                                    requirement=task)
            review = decision.to_dict()
            review["gate"] = "forge.security.review.ReviewGate"
        except Exception as exc:  # gate unavailable is itself a failure
            review = {"gate": "forge.security.review.ReviewGate",
                      "verdict": "ERROR", "approved": False,
                      "reason": "review gate unavailable: %s" % exc,
                      "findings": [], "changed_files": changed}
            review_failed = True
        try:
            from forge.security.verification import VerificationPipeline
            diff_gate = VerificationPipeline(self.root).review(
                diff_text, changed)
            review["diff_review"] = {
                "passed": bool(diff_gate.passed),
                "details": str(diff_gate.details)[:800],
                "evidence": dict(diff_gate.evidence or {})}
        except Exception as exc:
            review["diff_review"] = {
                "passed": False,
                "details": "diff review unavailable: %s" % exc}
        review["passed"] = (bool(review.get("approved", not review_failed))
                            and bool(review["diff_review"].get("passed",
                                                               True)))
        review_failed = not review["passed"]
        if not review_failed:
            findings = self.hub.respond(ReasoningRequest(
                kind=ReasoningKind.REVIEW, task=task,
                payload={"verification": report.verification}))
            if findings.ok:
                review["findings"] = findings.data
        report.review = review
        step.status = "done" if not review_failed else "failed"
        step.result = {"passed": not review_failed,
                       "verdict": review.get("verdict", "?")}
        self._event(report, "review_completed",
                    {"passed": not review_failed,
                     "verdict": review.get("verdict", "?")})
        return review_failed

    # -- supporting helpers ---------------------------------------------------------

    def _set_stage(self, report: NativeRunReport,
                   state: EngineState) -> None:
        self.tracker.set_state(state)
        value = EngineState(state).value
        if value not in report.stages:
            report.stages.append(value)
        self._event(report, "stage", {"stage": value})

    def _is_git_worktree(self) -> bool:
        if self._git_checked is None:
            try:
                import subprocess
                probe = subprocess.run(
                    ["git", "rev-parse", "--is-inside-work-tree"],
                    cwd=str(self.root), capture_output=True, text=True,
                    timeout=20, check=False)
                self._git_checked = probe.returncode == 0
            except Exception:
                self._git_checked = False
        return bool(self._git_checked)

    def _diff_text(self) -> str:
        if not self._is_git_worktree():
            return ""
        try:
            from forge.tools.git import GitTool
            git = GitTool(str(self.root))
            return (git.diff() + "\n" + git.status())[:60000]
        except Exception:
            return ""

    def _finish_abnormal(self, report: NativeRunReport, final: str,
                         task_state: TaskState, state: EngineState,
                         error: str,
                         applied_files: List[str]) -> None:
        if report.final_status == "PENDING":
            report.final_status = final
        report.error = error or report.error
        report.duration_seconds = perf_counter() - self._run_started
        self._rollback(sorted(set(applied_files)), report)
        self.tracker.set_state(state, error=report.error)
        self.tracker.end_task(task_state)

    def _update_backend_state(self, report: NativeRunReport) -> None:
        hub_status = self.hub.status()
        report.backends = hub_status
        report.model_backend = self._model_backend_state()
        self.tracker.set_backends(
            self._reasoning_backend_payload(hub_status),
            report.model_backend)

    def _memorize(self, task: str, plan: Any, verification: Any,
                  tests_ok: Optional[bool], debug_done: bool,
                  needs_model: bool, report: NativeRunReport) -> None:
        written: List[Dict[str, Any]] = []
        try:
            record = self.memory.record_decision(
                task,
                "task_class=%s confidence=%s steps=%d" % (
                    plan.task_class, plan.confidence, len(plan.steps)),
                reason="deterministic plan %s" % (
                    "validated" if not plan.validate() else "invalid"))
            written.append({"category": "decisions", "id": record.id})
            vrecord = self.memory.record_verification(
                task, verification.status_dict())
            written.append({"category": "verification", "id": vrecord.id})
            review_failed_now = bool(report.review) and \
                report.review.get("passed") is False
            if tests_ok is False or verification.status == "FAIL" or \
                    review_failed_now:
                categories = sorted({
                    str(cycle.get("classification", "unknown"))
                    for cycle in (report.debug or {}).get("cycles", [])})
                frecord = self.memory.record_failure(
                    task,
                    ",".join(categories)
                    or ("review" if review_failed_now else "verification"),
                    "verification gates failed: %s; tests: %s%s" % (
                        verification.failed,
                        "failed" if tests_ok is False else "passed",
                        "; review gate: failed" if review_failed_now
                        else ""))
                written.append({"category": "failures", "id": frecord.id})
            elif tests_ok and verification.status != "FAIL" and \
                    not needs_model and not review_failed_now:
                srecord = self.memory.record_strategy(
                    task,
                    "plan %s (%d steps, confidence %s) verified: %s%s" % (
                        plan.task_class, len(plan.steps), plan.confidence,
                        verification.status,
                        " after %d debug cycle(s)" % len(
                            (report.debug or {}).get("cycles", []))
                        if debug_done else ""),
                    evidence={"files_changed": len(report.files_changed),
                              "retries": report.retries})
                written.append({"category": "strategies", "id": srecord.id})
        except Exception as exc:
            report.notes.append("memory write failed: %s" % exc)
        report.memory = {"written": written,
                         "summary": self.memory.summary()}

    def _rollback(self, applied_files: List[str],
                  report: NativeRunReport) -> None:
        if not applied_files:
            return
        try:
            rolled = self.coding.rollback(sorted(set(applied_files)))
            report.rollback = bool(rolled)
            if rolled:
                report.files_changed = []
            self._event(report, "rollback",
                        {"files": sorted(set(applied_files)),
                         "restored": bool(rolled)})
        except Exception as exc:
            report.notes.append("rollback failed: %s" % exc)

    def _persist_run_record(self, report: NativeRunReport) -> None:
        """Persist a redacted run record under ``.forge/native/runs/``.

        These records are what :mod:`forge.native.training` turns into
        training datasets — only real executions land here, and raw model
        output is included only when dataset capture is explicitly enabled.
        """
        try:
            directory = self.root / RUNS_RELATIVE_DIR
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / ("%s.json" % report.run_id)
            path.write_text(json.dumps(report.to_dict(), indent=1,
                                       sort_keys=True, default=str),
                            encoding="utf-8")
        except OSError as exc:
            report.notes.append("run record not persisted: %s" % exc)


def read_run_record(root: str | Path,
                    run_id: str) -> Optional[Dict[str, Any]]:
    """Read a persisted run record (used by status/CLI/datasets)."""
    path = Path(root) / RUNS_RELATIVE_DIR / ("%s.json" % run_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def list_run_records(root: str | Path,
                     limit: int = 20) -> List[Dict[str, Any]]:
    """Newest-first bounded list of run-record summaries."""
    directory = Path(root) / RUNS_RELATIVE_DIR
    if not directory.is_dir():
        return []
    entries: List[Dict[str, Any]] = []
    for path in sorted(directory.glob("*.json"), reverse=True)[:limit * 4]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        entries.append({
            "run_id": data.get("run_id", path.stem),
            "final_status": data.get("final_status", "?"),
            "task_class": data.get("task_class", ""),
            "requirement": str(data.get("requirement", ""))[:120],
            "files_changed": len(data.get("files_changed") or []),
            "duration_seconds": data.get("duration_seconds", 0.0),
            "file": path.name,
        })
        if len(entries) >= max(1, int(limit)):
            break
    return entries
