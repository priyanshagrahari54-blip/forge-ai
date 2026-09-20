"""Task execution bridge with durable worker milestone observability."""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

from forge.core.run_control import SupervisorControl

STAGE_PROGRESS: Dict[str, Any] = {
    "PLAN": ("planning", 0.05), "AGENTS": ("planning", 0.10),
    "MODEL": ("model", 0.15), "CODE": ("coding", 0.30),
    "TEST": ("testing", 0.45), "DEBUG": ("debugging", 0.50),
    "REPAIR": ("debugging", 0.55), "RETEST": ("testing", 0.60),
    "REVIEW": ("review", 0.70), "SECURITY": ("security", 0.75),
    "BENCHMARK": ("benchmark", 0.80), "ACCEPTANCE": ("acceptance", 0.85),
    "CHECKPOINT": ("checkpoint", 0.90), "COMMIT": ("commit", 0.95),
    "COMPLETED": ("completed", 1.0), "ROLLBACK": ("rollback", 0.95),
}

SUPERVISOR_EVENTS: Dict[str, str] = {
    "task_started": "run.started", "agents_selected": "agent.selected",
    "model_selected": "model.selected", "change_proposed": "change.proposed",
    "permission_decision": "permission.checked", "change_applied": "changes.applied",
    "test_executed": "tests.executed", "test_failed": "tests.failed",
    "repair_attempted": "repair.attempted", "review_result": "review.completed",
    "security_result": "security.completed", "benchmark_result": "benchmark.completed",
    "acceptance_result": "acceptance.completed", "model_unavailable": "model.unavailable",
    "commit": "git.commit", "rollback": "rollback.completed", "cancelled": "task.cancelled",
}


class ExecutionContext:
    def __init__(self, server: Any, task: Any, project: Any,
                 control: SupervisorControl, checkpoint_id: str = "",
                 identity: Any = None, fence: Any = None) -> None:
        self.server, self.task, self.project = server, task, project
        self.control, self.checkpoint_id = control, checkpoint_id
        self.identity, self.fence = identity, fence
        self._milestone_controller = None
        try:
            from forge.orchestration.worker_milestones import WorkerMilestoneController
            self._milestone_controller = WorkerMilestoneController(project.root, task.task_id)
        except Exception:
            self._milestone_controller = None

    @property
    def attempt_id(self) -> str:
        if self.fence is not None:
            return str(getattr(self.fence, "attempt_id", "") or "")
        if self.identity is not None:
            return str(getattr(self.identity, "attempt_id", "") or "")
        return ""

    @property
    def commit_guard(self) -> Any:
        if self.fence is None:
            return None
        from forge.core.fencing import commit_guard
        return commit_guard(self.fence, getattr(self.server, "fences", None))

    def emit(self, event_type: str, data: Optional[Dict[str, Any]] = None) -> None:
        payload = data if isinstance(data, dict) else {}
        self.server.emit(self.task.task_id, self.task.project_id, event_type, payload)
        if self._milestone_controller is not None:
            try:
                self._milestone_controller.observe(event_type, payload)
            except Exception:
                pass

    def log(self, message: str, *, level: str = "info", source: str = "executor") -> None:
        self.server.log(self.task.task_id, self.task.project_id, message, level=level, source=source)

    def set_stage(self, raw_stage: str) -> None:
        display, progress = STAGE_PROGRESS.get(str(raw_stage).upper(), (str(raw_stage).lower(), None))
        updates: Dict[str, Any] = {"stage": display}
        if progress is not None:
            updates["progress"] = progress
        try:
            self.server.tasks.update(self.task.task_id, **updates)
        except Exception:
            pass
        self.emit("stage.started", {"stage": display, "raw": str(raw_stage)})

    @property
    def cancelled(self) -> bool:
        return self.control.cancel_requested

    def checkpoint(self, stage: str = "") -> None:
        self.control.checkpoint(stage)

    def request_approval(self, query: Any, *, model: str = "", provider: str = "") -> str:
        return self.server.request_approval(self.task, query, control=self.control, model=model, provider=provider)

    @property
    def root(self) -> str:
        return self.project.root

    def memory(self) -> Any:
        from forge.memory.store import MemoryStore
        import os
        return MemoryStore(os.path.join(self.project.root, ".forge", "memory"))


class TaskExecutor:
    def execute(self, ctx: ExecutionContext) -> Dict[str, Any]:
        raise NotImplementedError


class CallableExecutor(TaskExecutor):
    def __init__(self, fn: Callable[[ExecutionContext], Any]) -> None:
        if not callable(fn):
            raise ValueError("CallableExecutor needs a callable")
        self._fn = fn

    def execute(self, ctx: ExecutionContext) -> Dict[str, Any]:
        outcome = self._fn(ctx)
        if outcome is None:
            return {"accepted": True, "result": {}}
        if not isinstance(outcome, dict):
            return {"accepted": True, "result": {"value": str(outcome)}}
        return outcome if "accepted" in outcome else dict(outcome, accepted=True)


class SupervisorExecutor(TaskExecutor):
    def __init__(self, fabric: Any = None, *, model_policy: Any = None,
                 policy: Any = None, post_verify: bool = True) -> None:
        self.fabric, self.model_policy, self.policy, self.post_verify = fabric, model_policy, policy, post_verify

    def execute(self, ctx: ExecutionContext) -> Dict[str, Any]:
        from forge.core.supervisor import Supervisor
        from forge.security.permissions import OperationMode
        server = ctx.server
        fabric = self.fabric if self.fabric is not None else server.fabric
        identity = ctx.identity
        if identity is None:
            from forge.models.inference_path import ExecutionIdentity
            identity = ExecutionIdentity.new(str(getattr(ctx.task, "task_id", "") or ""),
                                             attempt=int(getattr(ctx.task, "retry_count", 0) or 0) + 1,
                                             boot_id=str(getattr(server, "boot_id", "") or ""))
        binder = getattr(fabric, "bind_identity", None)
        if callable(binder):
            fabric = binder(identity, fence=ctx.fence, fence_registry=getattr(server, "fences", None), commit_guard=ctx.commit_guard)
        try:
            mode = OperationMode(ctx.task.mode)
        except ValueError:
            mode = OperationMode.ASSISTED

        def on_event(name: str, details: Dict[str, Any]) -> None:
            try:
                if name == "stage_started":
                    ctx.set_stage(str(details.get("stage", "")))
                    return
                mapped = SUPERVISOR_EVENTS.get(name)
                if mapped:
                    ctx.emit(mapped, dict(details or {}))
            except Exception:
                pass

        def approval_callback(query: Any) -> str:
            return ctx.request_approval(query, model=str(getattr(ctx.task, "model", "") or ""), provider="")

        ctx.log("Starting Supervisor transaction (mode=%s, profile=%s)" % (mode.value, server.authorizer.profile), source="supervisor")
        path_snapshot = {}
        try:
            snapshot = getattr(fabric, "inference_path_snapshot", None)
            path_snapshot = snapshot() if callable(snapshot) else {}
        except Exception:
            pass
        ctx.emit("inference.path", {"mode": path_snapshot.get("mode", "legacy"), "attached": bool(path_snapshot.get("attached", False)),
                                     "task_id": str(getattr(ctx.task, "task_id", "") or ""), "attempt_id": ctx.attempt_id,
                                     "trace_id": str(getattr(identity, "trace_id", "") or ""), "boot_id": str(getattr(identity, "boot_id", "") or "")})
        supervisor = Supervisor(ctx.project.project_id, ctx.project.root)
        outcome = supervisor.run(ctx.task.requirement, approved=False, fabric=fabric, mode=mode, policy=self.policy,
                                 approval_store=server.approval_store, audit_log=server.audit, model_policy=self.model_policy,
                                 approval_callback=approval_callback, on_event=on_event, control=ctx.control,
                                 commit_guard=ctx.commit_guard)
        #: Terminal events are *not* emitted here. ``ctx.emit`` writes
        #: straight to the event log, so publishing "completed" before the
        #: worker's lease + attempt-fence gate would let a stale attempt
        #: announce a result it is not authorized to publish (and would emit
        #: a second, provenance-less copy of the event). The settlement path
        #: in ``forge.server.workers`` performs the gated transition and
        #: emits the single canonical terminal event with bounded inference
        #: provenance.
        return self._finalize(ctx, outcome, identity=identity)

    def _finalize(self, ctx: ExecutionContext, outcome: Dict[str, Any], identity: Any = None) -> Dict[str, Any]:
        files = [str(path) for path in outcome.get("files", []) if isinstance(path, str)][:500]
        report = outcome.get("report") or {}
        result = {"run_id": outcome.get("run_id", ""), "accepted": bool(outcome.get("accepted")),
                  "stages": outcome.get("stages", []), "gates": outcome.get("gates", []), "files": files,
                  "rollback": bool(outcome.get("rollback", False)), "checkpoint_id": outcome.get("checkpoint_id", "") or ctx.checkpoint_id,
                  "report": report, "duration_seconds": outcome.get("duration_seconds"), "model": outcome.get("model", ""),
                  "provider": outcome.get("provider") or outcome.get("selected_provider") or report.get("provider") or "",
                  "timings": outcome.get("timings", {}), "inference": _inference_provenance(outcome, ctx, identity), "attempt_id": ctx.attempt_id}
        if outcome.get("error"):
            result["error"] = str(outcome["error"])[:2000]
        try:
            from forge.tools.git import GitTool
            result["git"] = {"status": GitTool(ctx.root).status()}
        except Exception as exc:
            result["git"] = {"status": "", "error": str(exc)[:200]}
        if self.post_verify:
            try:
                from forge.security.verification import VerificationPipeline
                gate = VerificationPipeline(ctx.root).security(files or None)
                result["verification"] = {"gate": gate.name, "passed": gate.passed, "details": gate.details, "evidence": gate.evidence}
            except Exception as exc:
                result["verification"] = {"gate": "security", "passed": False, "details": {"error": str(exc)[:500]}, "evidence": []}
        return {"accepted": bool(outcome.get("accepted")), "cancelled": bool(outcome.get("cancelled")),
                "error": str(outcome.get("error", "") or "")[:2000], "files": files,
                "model": str(outcome.get("model", "") or ""), "provider": str(outcome.get("provider", "") or ""),
                "attempt_id": ctx.attempt_id, "inference": result.get("inference", {}), "result": result}


def _inference_provenance(outcome: Dict[str, Any], ctx: ExecutionContext, identity: Any = None) -> Dict[str, Any]:
    provenance: Dict[str, Any] = {}
    recorded = outcome.get("inference")
    if isinstance(recorded, dict):
        provenance.update(recorded)
    if identity is not None:
        for key, value in (("task_id", getattr(identity, "task_id", "")), ("attempt_id", getattr(identity, "attempt_id", "")),
                           ("trace_id", getattr(identity, "trace_id", "")), ("boot_id", getattr(identity, "boot_id", "")),
                           ("lease_owner", getattr(identity, "lease_owner", ""))):
            if value:
                provenance.setdefault(key, str(value))
    if ctx.fence is not None:
        provenance["fence_state"] = str(getattr(ctx.fence, "state", "") or "")
        provenance["fence_generation"] = int(getattr(ctx.fence, "generation", 0) or 0)
    snapshot = getattr(ctx.server.fabric, "inference_path_snapshot", None)
    if callable(snapshot):
        try:
            provenance.setdefault("mode", (snapshot() or {}).get("mode", ""))
        except Exception:
            pass
    return provenance
