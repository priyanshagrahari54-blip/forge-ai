"""Task execution: the bridge from server workers into Forge's engine (A81).

A worker never touches the repository directly. It builds an
:class:`ExecutionContext` and hands it to a :class:`TaskExecutor`. The
default executor runs the **existing** guarded Supervisor transaction,
which is where every promised integration actually happens:

* **Supervisor** — :meth:`forge.core.supervisor.Supervisor.run` is the
  only execution path (model → code → tests → review → security →
  acceptance → exact-file commit).
* **Model Fabric** — the server's fabric routes all model calls
  (registry, policy, telemetry, failover).
* **Native AI Engine** — the default fabric registers the built-in
  local provider (``local-fallback``): a deterministic, offline engine
  so the server functions without any external service, and fails with
  an actionable diagnosis when a task genuinely needs a real model.
* **Runtime** — the Supervisor builds the permission-gated tool runtime
  (``forge.runtime.defaults.create_default_runtime``) for its agents.
* **Memory** — outcome summaries are recorded into the project's
  durable :class:`~forge.memory.store.MemoryStore`.
* **Checkpoints** — the worker snapshots the worktree before the run
  (:class:`~forge.tools.checkpoint.CheckpointManager`); the Supervisor
  checkpoints again internally and rolls back candidate-scoped on
  failure or cancellation.
* **Git** — commits go through :class:`~forge.tools.git.GitTool`'s
  exact-file, acceptance-gated path; the executor records the post-run
  worktree status.
* **Verification** — the Supervisor runs the verification pipeline
  inside the transaction; the executor additionally records a post-run
  security-gate summary in the task result.

Executors are pluggable (``ServerConfig.executor``); tests and scripted
automation use :class:`CallableExecutor` behind the identical contract.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

from forge.core.run_control import SupervisorControl

#: Raw Supervisor stages → (display stage, progress fraction).
STAGE_PROGRESS: Dict[str, Any] = {
    "PLAN": ("planning", 0.05),
    "AGENTS": ("planning", 0.10),
    "MODEL": ("model", 0.15),
    "CODE": ("coding", 0.30),
    "TEST": ("testing", 0.45),
    "DEBUG": ("debugging", 0.50),
    "REPAIR": ("debugging", 0.55),
    "RETEST": ("testing", 0.60),
    "REVIEW": ("review", 0.70),
    "SECURITY": ("security", 0.75),
    "BENCHMARK": ("benchmark", 0.80),
    "ACCEPTANCE": ("acceptance", 0.85),
    "CHECKPOINT": ("checkpoint", 0.90),
    "COMMIT": ("commit", 0.95),
    "COMPLETED": ("completed", 1.0),
    "ROLLBACK": ("rollback", 0.95),
}

#: Supervisor report events → server event types.
SUPERVISOR_EVENTS: Dict[str, str] = {
    "task_started": "run.started",
    "agents_selected": "agent.selected",
    "model_selected": "model.selected",
    "change_proposed": "change.proposed",
    "permission_decision": "permission.checked",
    "change_applied": "changes.applied",
    "test_executed": "tests.executed",
    "test_failed": "tests.failed",
    "repair_attempted": "repair.attempted",
    "review_result": "review.completed",
    "security_result": "security.completed",
    "benchmark_result": "benchmark.completed",
    "acceptance_result": "acceptance.completed",
    "model_unavailable": "model.unavailable",
    "commit": "git.commit",
    "rollback": "rollback.completed",
    "cancelled": "task.cancelled",
}


class ExecutionContext:
    """Everything an executor may use, and nothing more.

    The context deliberately exposes no shell, no path outside the
    project root, and no direct database access: emitting events,
    writing logs, advancing the stage, requesting approvals, and reading
    the task/project are the whole surface.
    """

    def __init__(self, server: Any, task: Any, project: Any,
                 control: SupervisorControl, checkpoint_id: str = "") -> None:
        self.server = server
        self.task = task
        self.project = project
        self.control = control
        self.checkpoint_id = checkpoint_id

    # -- observability ---------------------------------------------------------

    def emit(self, event_type: str, data: Optional[Dict[str, Any]] = None) -> None:
        self.server.emit(self.task.task_id, self.task.project_id,
                         event_type, data)

    def log(self, message: str, *, level: str = "info",
            source: str = "executor") -> None:
        self.server.log(self.task.task_id, self.task.project_id, message,
                        level=level, source=source)

    def set_stage(self, raw_stage: str) -> None:
        display, progress = STAGE_PROGRESS.get(
            str(raw_stage).upper(), (str(raw_stage).lower(), None))
        updates: Dict[str, Any] = {"stage": display}
        if progress is not None:
            updates["progress"] = progress
        try:
            self.server.tasks.update(self.task.task_id, **updates)
        except Exception:
            pass  # stage bookkeeping must never break the run
        self.emit("stage.started", {"stage": display, "raw": str(raw_stage)})

    # -- control -----------------------------------------------------------------

    @property
    def cancelled(self) -> bool:
        return self.control.cancel_requested

    def checkpoint(self, stage: str = "") -> None:
        """Cooperative pause/cancel boundary (raises TaskCancelled)."""
        self.control.checkpoint(stage)

    # -- approvals ------------------------------------------------------------------

    def request_approval(self, query: Any, *, model: str = "",
                         provider: str = "") -> str:
        """File approval request(s) and block until decided.

        Returns an A33 approval-token id, or ``""`` on denial/expiry
        (fail closed). Raises :class:`TaskCancelled` if the task is
        cancelled while waiting.
        """
        return self.server.request_approval(self.task, query, control=self.control,
                                            model=model, provider=provider)

    # -- project services ---------------------------------------------------------------

    @property
    def root(self) -> str:
        return self.project.root

    def memory(self) -> Any:
        """The project's durable memory store (bounded, path-safe)."""
        from forge.memory.store import MemoryStore

        import os

        return MemoryStore(os.path.join(self.project.root, ".forge", "memory"))


class TaskExecutor:
    """Executor contract: ``execute(ctx) -> outcome dict``.

    The outcome dict must contain ``accepted`` (bool) and may contain
    ``result`` (JSON-safe dict), ``error`` (str), ``cancelled`` (bool),
    ``files`` (list[str]), ``model``/``provider`` (str).
    """

    def execute(self, ctx: ExecutionContext) -> Dict[str, Any]:
        raise NotImplementedError


class CallableExecutor(TaskExecutor):
    """Adapts a plain ``fn(ctx) -> dict`` into the executor contract.

    Used by tests and scripted automation; the server's guarantees
    (queue, persistence, policy admission, events, approvals) are
    identical regardless of executor.
    """

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
        if "accepted" not in outcome:
            outcome = dict(outcome, accepted=True)
        return outcome


class SupervisorExecutor(TaskExecutor):
    """Runs the real guarded Supervisor transaction for one task.

    Mirrors the A34 :class:`~forge.cockpit.plane.ControlPlane` semantics:
    the fine-grained A33 policy is enforced at *admission* (see
    :class:`~forge.server.authorization.Authorizer`), while the run itself
    executes under A32 mode semantics — every mutation boundary (change
    set, git commit) stops for an operator decision through the durable
    approval bridge. An explicit ``policy`` may be attached for stricter
    runs, but note that terminal-level enforcement has no interactive
    approval channel, so an assisted-profile policy would block test
    execution outright (fail closed).
    """

    def __init__(self, fabric: Any = None, *,
                 model_policy: Any = None,
                 policy: Any = None,
                 post_verify: bool = True) -> None:
        self.fabric = fabric
        self.model_policy = model_policy
        self.policy = policy
        self.post_verify = post_verify

    def execute(self, ctx: ExecutionContext) -> Dict[str, Any]:
        from forge.core.supervisor import Supervisor
        from forge.security.permissions import OperationMode
        from forge.tools.git import GitTool

        server = ctx.server
        fabric = self.fabric if self.fabric is not None else server.fabric
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
                if mapped is None:
                    return
                ctx.emit(mapped, dict(details or {}))
            except Exception:
                pass  # live observability must never break the run

        def approval_callback(query: Any) -> str:
            return ctx.request_approval(
                query,
                model=str(getattr(ctx.task, "model", "") or ""),
                provider="")

        ctx.log("Starting Supervisor transaction (mode=%s, profile=%s)"
                % (mode.value, server.authorizer.profile),
                source="supervisor")
        supervisor = Supervisor(ctx.project.project_id, ctx.project.root)
        outcome = supervisor.run(
            ctx.task.requirement,
            approved=False,
            fabric=fabric,
            mode=mode,
            policy=self.policy,
            approval_store=server.approval_store,
            audit_log=server.audit,
            model_policy=self.model_policy,
            approval_callback=approval_callback,
            on_event=on_event,
            control=ctx.control,
        )
        return self._finalize(ctx, outcome)

    def _finalize(self, ctx: ExecutionContext,
                  outcome: Dict[str, Any]) -> Dict[str, Any]:
        """Attach post-run Git + Verification evidence to the outcome."""
        files = [str(path) for path in outcome.get("files", [])
                 if isinstance(path, str)][:500]
        report = outcome.get("report") or {}
        result: Dict[str, Any] = {
            "run_id": outcome.get("run_id", ""),
            "accepted": bool(outcome.get("accepted")),
            "stages": outcome.get("stages", []),
            "gates": outcome.get("gates", []),
            "files": files,
            "rollback": bool(outcome.get("rollback", False)),
            "checkpoint_id": outcome.get("checkpoint_id", "")
            or ctx.checkpoint_id,
            "report": report,
            "duration_seconds": outcome.get("duration_seconds"),
            "model": outcome.get("model", ""),
            # The Supervisor reports the provider as ``selected_provider``
            # (routing) and on the report; there is no bare ``provider``
            # key, so resolve it in that order.
            "provider": (outcome.get("provider")
                         or outcome.get("selected_provider")
                         or report.get("provider") or ""),
            "timings": outcome.get("timings", {}),
        }
        if outcome.get("error"):
            result["error"] = str(outcome["error"])[:2000]
        # Git: post-run worktree state (read-only inspection).
        try:
            from forge.tools.git import GitTool

            result["git"] = {"status": GitTool(ctx.root).status()}
        except Exception as exc:
            result["git"] = {"status": "", "error": str(exc)[:200]}
        # Verification: post-run security-gate summary over the changed
        # files (the full pipeline already ran inside the transaction).
        if self.post_verify:
            try:
                from forge.security.verification import VerificationPipeline

                gate = VerificationPipeline(ctx.root).security(files or None)
                result["verification"] = {
                    "gate": gate.name, "passed": gate.passed,
                    "details": gate.details, "evidence": gate.evidence}
            except Exception as exc:
                result["verification"] = {
                    "gate": "security", "passed": False,
                    "details": {"error": str(exc)[:500]}, "evidence": []}
        return {
            "accepted": bool(outcome.get("accepted")),
            "cancelled": bool(outcome.get("cancelled")),
            "error": str(outcome.get("error", "") or "")[:2000],
            "files": files,
            "model": str(outcome.get("model", "") or ""),
            "provider": str(outcome.get("provider", "") or ""),
            "result": result,
        }
