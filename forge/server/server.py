"""The Forge Server composition root (A81).

:class:`ForgeServer` wires the whole backend together::

    Client
      → Authentication   (auth.AuthManager: API keys, sessions, bootstrap)
      → Authorization    (authorization.Authorizer: scopes + A33 policy)
      → API Gateway      (api.create_app: validated, bounded, fail-closed)
      → Task Queue       (queue.TaskQueue: persistent, leased, recoverable)
      → Scheduler        (scheduler.Scheduler: dispatch loop)
      → Workers          (workers.WorkerPool: background threads)
      → Agents           (executor → Supervisor → Coder/Debugger/Reviewer/…)
      → Model Runtime    (Model Fabric + native local engine fallback)
      → Verification     (pipeline gates inside the run + post-run summary)
      → Result Store     (tasks/events/logs/approvals/notifications in SQLite)

Everything durable lives in one SQLite database, so the server can be
killed and restarted: queued work stays queued, interrupted work is
re-queued within its retry budget (or failed honestly), sessions and
events survive, and reconnecting clients recover the full picture
through :class:`~forge.server.reconnect.RecoveryService`.

The server is deliberately *not* a remote shell: see
:mod:`forge.server.authorization` for the guarantee and the tests that
lock it in.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from forge.security.approvals import ApprovalStore
from forge.security.audit import AuditLog
from forge.security.policy import PermissionPolicy
from forge.server.approvals import ApprovalManager
from forge.server.auth import AuthManager
from forge.server.authorization import Authorizer
from forge.server.errors import (
    Conflict,
    InvalidRequest,
    InvalidTransition,
    ServerError,
)
from forge.server.events import EventStore
from forge.server.health import HealthMonitor
from forge.server.logs import LogStore
from forge.server.models import (
    MAX_REQUIREMENT_CHARS,
    ServerTask,
    TaskStatus,
    validate_id,
)
from forge.server.notifications import NotificationService
from forge.server.projects import ProjectRegistry
from forge.server.queue import TaskQueue
from forge.server.reconnect import RecoveryService
from forge.server.scheduler import Scheduler
from forge.server.sessions import SessionManager
from forge.server.storage import Database
from forge.server.tasks import TaskManager
from forge.server.workers import WorkerPool

#: Bump on any recovery-bundle shape change so clients can detect it.
PROTOCOL_VERSION = 1


@dataclass
class ServerConfig:
    """Forge Server configuration (env-overridable, no secrets in files)."""

    db_path: "str | Path" = ".forge/server/server.db"
    host: str = "127.0.0.1"
    port: int = 8300
    projects: "Dict[str, str]" = field(default_factory=dict)
    #: name → role for API keys created at first start (raw keys are
    #: generated, printed once, and never stored).
    api_keys: "Dict[str, str]" = field(default_factory=dict)
    #: Single startup admin token; empty means "none configured".
    bootstrap_token: str = ""
    #: A33 permission profile: safe | assisted | autonomous | locked.
    profile: str = "assisted"
    #: Custom A33 policy (overrides the profile-built one).
    policy: Optional[PermissionPolicy] = None
    #: Model Fabric instance; None builds the default (native local
    #: engine + Ollama/OpenAI from env when configured).
    fabric: Any = None
    #: Task executor; None uses the real SupervisorExecutor.
    executor: Any = None
    max_workers: int = 4
    #: Resource governor profile: "" (auto-detect) | default | g560.
    #: Distinct from ``profile`` above, which is the A33 *permission*
    #: profile. The resource profile can only reduce ``max_workers``.
    resource_profile: str = ""
    max_tasks_per_project: int = 1
    default_max_retries: int = 2
    retry_backoff_seconds: float = 2.0
    approval_timeout: float = 600.0
    approval_token_ttl: float = 300.0
    session_ttl: float = 12 * 3600
    max_sessions: int = 200
    max_events_per_task: int = 5000
    max_logs_per_task: int = 2000
    checkpoint_retention: int = 10
    memory_record_tasks: bool = True
    local_dev_mode: bool = True
    allowed_origins: "List[str]" = field(default_factory=list)
    poll_interval: float = 0.25

    @classmethod
    def from_env(cls, **overrides: Any) -> "ServerConfig":
        """Build config with ``FORGE_SERVER_`` environment overrides."""

        def env_float(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, default))
            except ValueError:
                return default

        def env_int(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, default))
            except ValueError:
                return default

        config = cls(
            db_path=os.environ.get("FORGE_SERVER_DB",
                                   ".forge/server/server.db"),
            host=os.environ.get("FORGE_SERVER_HOST", "127.0.0.1"),
            port=env_int("FORGE_SERVER_PORT", 8300),
            bootstrap_token=os.environ.get("FORGE_SERVER_TOKEN", ""),
            profile=os.environ.get("FORGE_SERVER_PROFILE", "assisted"),
            resource_profile=os.environ.get("FORGE_RESOURCE_PROFILE", ""),
            max_workers=env_int("FORGE_SERVER_MAX_WORKERS", 4),
            max_tasks_per_project=env_int(
                "FORGE_SERVER_MAX_TASKS_PER_PROJECT", 1),
            default_max_retries=env_int("FORGE_SERVER_MAX_RETRIES", 2),
            retry_backoff_seconds=env_float(
                "FORGE_SERVER_RETRY_BACKOFF", 2.0),
            approval_timeout=env_float("FORGE_SERVER_APPROVAL_TIMEOUT",
                                       600.0),
            session_ttl=env_float("FORGE_SERVER_SESSION_TTL", 12 * 3600),
            local_dev_mode=os.environ.get(
                "FORGE_SERVER_AUTH_MODE", "local-dev") != "production",
        )
        for key, value in overrides.items():
            setattr(config, key, value)
        return config


class ForgeServer:
    """The standalone Forge Server backend."""

    def __init__(self, config: Optional[ServerConfig] = None) -> None:
        self.config = config or ServerConfig()
        self.boot_id = uuid4().hex[:12]
        self.started_at: Optional[float] = None
        self._stopping = threading.Event()

        # -- storage layer --------------------------------------------------
        self.db = Database(self.config.db_path)

        # -- durable stores ---------------------------------------------------
        self.tasks = TaskManager(
            self.db, default_max_retries=self.config.default_max_retries)
        self.queue = TaskQueue(self.db)
        self.events = EventStore(
            self.db, max_events_per_task=self.config.max_events_per_task)
        self.logs = LogStore(
            self.db, max_logs_per_task=self.config.max_logs_per_task)
        self.sessions = SessionManager(
            self.db, max_sessions=self.config.max_sessions,
            ttl_seconds=self.config.session_ttl)
        self.projects = ProjectRegistry(self.db)
        self.notifications = NotificationService(self.db)

        # -- security layer (A33) -----------------------------------------------
        self.authorizer = Authorizer(
            self.config.profile, policy=self.config.policy)
        self.auth = AuthManager(
            self.db, self.sessions,
            bootstrap_token=self.config.bootstrap_token)
        self.approval_store = ApprovalStore()
        db_dir = Path(self.config.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)
        self.audit = AuditLog(sink_path=db_dir / "audit.jsonl")
        self.approvals = ApprovalManager(
            self.db, self.approval_store, emit=self.emit,
            notify=self.notify,
            approval_timeout=self.config.approval_timeout,
            token_ttl=self.config.approval_token_ttl)

        # -- execution layer ------------------------------------------------------
        if self.config.fabric is not None:
            self.fabric = self.config.fabric
        else:
            from forge.models.fabric import ModelFabric

            self.fabric = ModelFabric.from_defaults()
        if self.config.executor is not None:
            self.executor = self.config.executor
        else:
            from forge.server.executor import SupervisorExecutor

            self.executor = SupervisorExecutor(fabric=self.fabric)

        # -- background machinery ---------------------------------------------------
        from forge.core.resource_governor import ResourceGovernor, select_profile
        self.governor = ResourceGovernor(select_profile(
            self.config.resource_profile))
        self.pool = WorkerPool(
            max_workers=self.governor.clamp_workers(self.config.max_workers))
        self.scheduler = Scheduler(
            self, poll_interval=self.config.poll_interval)
        self.health_monitor = HealthMonitor(self)
        self.recovery = RecoveryService(self)

        # -- in-memory, per-boot state ------------------------------------------------
        self._controls: Dict[str, Any] = {}
        self._controls_lock = threading.Lock()
        self._checkpoints: "Dict[str, Any]" = {}
        self._checkpoint_lock = threading.Lock()
        self._started = False

        # Configured projects are registered at construction (and again,
        # idempotently, at start) so submissions validate even before the
        # background machinery is up.
        for project_id, root in (self.config.projects or {}).items():
            self.projects.register(project_id, root)

    # -- lifecycle ---------------------------------------------------------------------

    def start(self) -> "ForgeServer":
        """Start background workers + scheduler (idempotent)."""
        if self._started:
            return self
        self._stopping.clear()
        # Register configured projects (idempotent).
        for project_id, root in (self.config.projects or {}).items():
            self.projects.register(project_id, root)
        # First-boot API keys.
        for name, role in (self.config.api_keys or {}).items():
            existing = [key for key in self.auth.list_api_keys()
                        if key["name"] == name and not key["revoked"]]
            if not existing:
                self.auth.create_api_key(name, role)
        # Restart hygiene: dead leases, dead sessions, dead approvals.
        stale_leases = self.queue.recover()
        self.sessions.prune()
        expired = self.approvals.expire_all_pending(
            "server restarted; the task will re-ask if still needed")
        recovered = self.tasks.recover_interrupted()
        for record in recovered:
            task = self.tasks.get(record["task_id"])
            if task is None:
                continue
            if record["action"] == "requeued":
                self.queue.enqueue(
                    task.task_id, task.project_id, priority=task.priority)
                self.emit(task.task_id, task.project_id, "task.requeued",
                          {"reason": "server_restart",
                           "retry_count": record["retry_count"]})
                self.log(task.task_id, task.project_id,
                         "Re-queued after server restart (attempt %d)."
                         % (record["retry_count"] + 1),
                         level="warning", source="recovery")
            else:
                self.emit(task.task_id, task.project_id, "task.failed",
                          {"reason": "server_restart",
                           "detail": "Retry budget exhausted."})
                self.notify(task.project_id, "task.failed",
                            "Task %s failed" % task.task_id,
                            "Interrupted by server restart; retry budget "
                            "exhausted.", task_id=task.task_id)
        self.db.set_state("boot_id", self.boot_id)
        self.db.set_state("started_at", repr(time.time()))
        self.started_at = time.time()
        self.pool.start()
        self.scheduler.start()
        self._started = True
        self.emit("", "", "server.started",
                  {"boot_id": self.boot_id,
                   "profile": self.config.profile,
                   "recovered_tasks": len(recovered),
                   "stale_leases": stale_leases,
                   "expired_approvals": expired,
                   "protocol_version": PROTOCOL_VERSION})
        self.scheduler.wake()
        return self

    def stop(self, *, wait: bool = True) -> None:
        """Stop the scheduler and worker pool. Durable state is untouched."""
        self._stopping.set()
        self.scheduler.stop()
        self.pool.shutdown(wait=wait)
        if self._started:
            self.emit("", "", "server.stopped",
                      {"boot_id": self.boot_id, "wait": wait})
        self._started = False

    def close(self) -> None:
        """Stop and release the database (end of process)."""
        self.stop(wait=True)
        self.release_checkpoints()
        self.db.close()

    @property
    def running(self) -> bool:
        return self._started and self.pool.alive

    def __enter__(self) -> "ForgeServer":
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- observability plumbing used by workers/executors ------------------------------

    def emit(self, task_id: str, project_id: str, event_type: str,
             data: Optional[Dict[str, Any]] = None) -> None:
        try:
            self.events.append(task_id, project_id, event_type, data)
        except Exception:
            pass  # events must never break execution

    def log(self, task_id: str, project_id: str, message: str, *,
            level: str = "info", source: str = "") -> None:
        try:
            self.logs.append(task_id, project_id, message, level=level,
                             source=source)
        except Exception:
            pass  # logs must never break execution

    def notify(self, project_id: str, kind: str, title: str,
               body: str = "", *, task_id: str = "") -> None:
        try:
            self.notifications.notify(project_id, kind, title, body,
                                      task_id=task_id)
        except Exception:
            pass  # notifications must never break execution

    # -- control/checkpoint registries (per boot) ---------------------------------------

    def register_control(self, task_id: str, control: Any) -> None:
        with self._controls_lock:
            self._controls[task_id] = control

    def unregister_control(self, task_id: str) -> None:
        with self._controls_lock:
            self._controls.pop(task_id, None)

    def control_for(self, task_id: str) -> Any:
        with self._controls_lock:
            return self._controls.get(task_id)

    def hold_checkpoint(self, task_id: str, checkpoint: Any) -> None:
        with self._checkpoint_lock:
            self._checkpoints[task_id] = checkpoint
            while len(self._checkpoints) > max(
                    1, int(self.config.checkpoint_retention)):
                oldest_id = next(iter(self._checkpoints))
                oldest = self._checkpoints.pop(oldest_id)
                try:
                    from forge.tools.checkpoint import CheckpointManager

                    CheckpointManager(".").cleanup(oldest)
                except Exception:
                    pass

    def held_checkpoint(self, task_id: str) -> Any:
        with self._checkpoint_lock:
            return self._checkpoints.get(task_id)

    def release_checkpoints(self) -> None:
        with self._checkpoint_lock:
            checkpoints = list(self._checkpoints.values())
            self._checkpoints.clear()
        for checkpoint in checkpoints:
            try:
                from forge.tools.checkpoint import CheckpointManager

                CheckpointManager(".").cleanup(checkpoint)
            except Exception:
                pass

    def expire_task_approvals(self, task_id: str) -> None:
        """Terminal tasks cannot keep approvals pending."""
        try:
            for record in self.approvals.list_for_task(task_id):
                if record["status"] == "pending":
                    self.approvals._set_status(  # noqa: SLF001 - internal
                        record["approval_id"], "expired")
                    self.emit(task_id, record["project_id"],
                              "approval.expired",
                              {"approval_id": record["approval_id"],
                               "reason": "task_finished"})
        except Exception:
            pass

    def record_task_memory(self, task_id: str) -> None:
        """Write a bounded outcome summary into durable project memory."""
        if not self.config.memory_record_tasks:
            return
        try:
            task = self.tasks.get(task_id)
            if task is None or not task.terminal:
                return
            project = self.projects.get(task.project_id)
            if project is None:
                return
            from forge.memory.store import MemoryStore

            result = task.result()
            summary = {
                "task_id": task.task_id,
                "project_id": task.project_id,
                "status": task.status.value,
                "stage": task.stage,
                "retry_count": task.retry_count,
                "accepted": bool(result.get("accepted", False)),
                "model": result.get("model", ""),
                "provider": result.get("provider", ""),
                "files_changed": len(result.get("files", []) or []),
                "error": task.error[:500],
                "finished_at": task.finished_at,
                "boot_id": self.boot_id,
            }
            store = MemoryStore(
                os.path.join(project.root, ".forge", "memory"))
            store.save("server/tasks/%s.json" % task.task_id,
                       json.dumps(summary, default=str))
        except Exception:
            pass  # memory is observability; it must never break the server

    # -- task operations (used by the API and the CLI) ----------------------------------

    def submit_task(self, project_id: str, requirement: str, *,
                    priority: int = 0, mode: Any = "", actor: str = "",
                    max_retries: Optional[int] = None) -> ServerTask:
        """Validate → admit (A33) → persist → enqueue → wake scheduler."""
        project = self.projects.get_or_raise(project_id)
        if not isinstance(requirement, str) or not requirement.strip():
            raise InvalidRequest("requirement must be a non-empty string.")
        if len(requirement) > MAX_REQUIREMENT_CHARS:
            raise InvalidRequest(
                "requirement exceeds %d characters." % MAX_REQUIREMENT_CHARS)
        effective_mode = self.authorizer.operation_mode(mode)
        self.authorizer.ensure_task_permitted(
            project.root, effective_mode,
            project_id=project.project_id, audit=self.audit)
        task = self.tasks.create(
            project_id, requirement, priority=priority,
            mode=effective_mode.value, actor=actor,
            max_retries=max_retries)
        self.emit(task.task_id, project_id, "task.created",
                  {"actor": actor, "mode": task.mode,
                   "priority": task.priority})
        self.log(task.task_id, project_id,
                 "Task created by %s (mode=%s)."
                 % (actor or "unknown", task.mode), source="server")
        self.tasks.transition(task.task_id, TaskStatus.QUEUED,
                              expected=(TaskStatus.CREATED,))
        self.queue.enqueue(task.task_id, project_id, priority=priority)
        self.emit(task.task_id, project_id, "task.queued",
                  {"position": self.queue.position(task.task_id)})
        self.scheduler.wake()
        return self.tasks.get_or_raise(task.task_id)

    def cancel_task(self, task_id: str, *, actor: str = "",
                    expected_version: Optional[int] = None) -> ServerTask:
        task = self.tasks.get_or_raise(task_id)
        if (expected_version is not None
                and int(expected_version) != task.version):
            raise Conflict(
                "Task version changed: expected %d, found %d."
                % (int(expected_version), task.version),
                task_id=task_id, version=task.version)
        if task.terminal:
            raise Conflict(
                "Task is already %s." % task.status.value,
                task_id=task_id, status=task.status.value)
        control = self.control_for(task_id)
        if control is None and task.status in (TaskStatus.QUEUED,
                                               TaskStatus.PAUSED):
            # Not started yet (or paused pre-dispatch): cancel outright.
            updated = self.tasks.transition(
                task_id, TaskStatus.CANCELLED, expected=(task.status,),
                error="Cancelled before start.", stage="cancelled")
            self.queue.remove(task_id)
            self.expire_task_approvals(task_id)
            self.emit(task_id, task.project_id, "task.cancelled",
                      {"actor": actor, "detail": "cancelled_before_start"})
            self.log(task_id, task.project_id,
                     "Cancelled before start by %s." % (actor or "unknown"),
                     source="server")
            self.notify(task.project_id, "task.cancelled",
                        "Task %s cancelled" % task_id,
                        "Cancelled before start.", task_id=task_id)
            self.scheduler.wake()
            return updated
        # Cooperative cancellation at the next stage boundary.
        self.tasks.update(task_id, cancel_requested=True)
        if control is not None:
            control.request_cancel()
        self.emit(task_id, task.project_id, "task.cancelling",
                  {"actor": actor, "status": task.status.value})
        self.log(task_id, task.project_id,
                 "Cancellation requested by %s; the worker stops at the "
                 "next stage boundary." % (actor or "unknown"),
                 source="server")
        return self.tasks.get_or_raise(task_id)

    def pause_task(self, task_id: str, *, actor: str = "",
                   expected_version: Optional[int] = None) -> ServerTask:
        task = self.tasks.get_or_raise(task_id)
        if (expected_version is not None
                and int(expected_version) != task.version):
            raise Conflict(
                "Task version changed: expected %d, found %d."
                % (int(expected_version), task.version),
                task_id=task_id, version=task.version)
        if task.terminal:
            raise Conflict("Task is already %s." % task.status.value,
                           task_id=task_id)
        if task.status == TaskStatus.PAUSED:
            raise Conflict("Task is already paused.", task_id=task_id)
        control = self.control_for(task_id)
        if control is not None and task.status in (
                TaskStatus.STARTED, TaskStatus.RUNNING,
                TaskStatus.WAITING_FOR_APPROVAL):
            self.tasks.update(task_id, pause_requested=True)
            control.request_pause()
            self.emit(task_id, task.project_id, "task.pausing",
                      {"actor": actor})
            self.log(task_id, task.project_id,
                     "Pause requested by %s; the worker pauses at the "
                     "next stage boundary." % (actor or "unknown"),
                     source="server")
            return self.tasks.get_or_raise(task_id)
        if task.status == TaskStatus.QUEUED:
            updated = self.tasks.transition(
                task_id, TaskStatus.PAUSED, expected=(TaskStatus.QUEUED,),
                stage="paused")
            self.queue.remove(task_id)
            self.emit(task_id, task.project_id, "task.paused",
                      {"actor": actor, "detail": "paused_while_queued"})
            self.log(task_id, task.project_id,
                     "Paused while queued by %s." % (actor or "unknown"),
                     source="server")
            self.scheduler.wake()
            return updated
        raise Conflict(
            "Cannot pause task in status %s." % task.status.value,
            task_id=task_id)

    def resume_task(self, task_id: str, *, actor: str = "",
                    expected_version: Optional[int] = None) -> ServerTask:
        task = self.tasks.get_or_raise(task_id)
        if (expected_version is not None
                and int(expected_version) != task.version):
            raise Conflict(
                "Task version changed: expected %d, found %d."
                % (int(expected_version), task.version),
                task_id=task_id, version=task.version)
        if task.status != TaskStatus.PAUSED:
            raise Conflict(
                "Only paused tasks can resume (task is %s)."
                % task.status.value, task_id=task_id)
        control = self.control_for(task_id)
        if control is not None:
            self.tasks.update(task_id, pause_requested=False)
            control.request_resume()
            self.emit(task_id, task.project_id, "task.resuming",
                      {"actor": actor})
            self.log(task_id, task.project_id,
                     "Resume requested by %s." % (actor or "unknown"),
                     source="server")
            return self.tasks.get_or_raise(task_id)
        updated = self.tasks.transition(
            task_id, TaskStatus.QUEUED, expected=(TaskStatus.PAUSED,),
            pause_requested=False, stage="requeued")
        self.queue.enqueue(task.task_id, task.project_id,
                           priority=task.priority)
        self.emit(task_id, task.project_id, "task.resumed",
                  {"actor": actor, "detail": "requeued"})
        self.log(task_id, task.project_id,
                 "Resumed and re-queued by %s." % (actor or "unknown"),
                 source="server")
        self.scheduler.wake()
        return updated

    def retry_task(self, task_id: str, *, actor: str = "") -> ServerTask:
        task = self.tasks.get_or_raise(task_id)
        if task.status not in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            raise Conflict(
                "Only failed or cancelled tasks can be retried (task is "
                "%s)." % task.status.value, task_id=task_id)
        updated = self.tasks.transition(
            task_id, TaskStatus.QUEUED, expected=(task.status,),
            error="", stage="retry_scheduled", cancel_requested=False,
            pause_requested=False, finished_at=None)
        self.queue.enqueue(task_id, task.project_id, priority=task.priority)
        self.emit(task_id, task.project_id, "task.retried",
                  {"actor": actor, "retry_count": updated.retry_count})
        self.log(task_id, task.project_id,
                 "Manual retry requested by %s." % (actor or "unknown"),
                 source="server")
        self.scheduler.wake()
        return updated

    def rollback_task(self, task_id: str, *, actor: str = "",
                      expected_version: Optional[int] = None) -> ServerTask:
        """Restore the pre-run checkpoint of a finished task."""
        task = self.tasks.get_or_raise(task_id)
        if (expected_version is not None
                and int(expected_version) != task.version):
            raise Conflict(
                "Task version changed: expected %d, found %d."
                % (int(expected_version), task.version),
                task_id=task_id, version=task.version)
        if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED,
                               TaskStatus.CANCELLED):
            raise Conflict(
                "Only finished tasks can be rolled back (task is %s)."
                % task.status.value, task_id=task_id)
        checkpoint = self.held_checkpoint(task_id)
        if checkpoint is None:
            raise Conflict(
                "The pre-run checkpoint is not held by this server "
                "process (checkpoints live in memory per boot); rollback "
                "is unavailable after a restart.", task_id=task_id)
        project = self.projects.get_or_raise(task.project_id)
        files = [str(path) for path in (task.result().get("files") or [])
                 if isinstance(path, str)]
        from forge.tools.checkpoint import CheckpointManager

        try:
            CheckpointManager(project.root).rollback(
                checkpoint, files or None)
        except Exception as exc:
            raise Conflict(
                "Rollback failed: %s" % exc, task_id=task_id) from exc
        with self._checkpoint_lock:
            self._checkpoints.pop(task_id, None)
        updated = self.tasks.transition(
            task_id, TaskStatus.ROLLED_BACK,
            expected=(TaskStatus.COMPLETED, TaskStatus.FAILED,
                      TaskStatus.CANCELLED),
            stage="rolled_back")
        self.emit(task_id, task.project_id, "task.rolled_back",
                  {"actor": actor, "files": files,
                   "checkpoint_id": task.checkpoint_id})
        self.log(task_id, task.project_id,
                 "Rolled back to pre-run checkpoint by %s (%d candidate "
                 "file(s))." % (actor or "unknown", len(files)),
                 source="server")
        self.notify(task.project_id, "task.rolled_back",
                    "Task %s rolled back" % task_id,
                    "Pre-run state restored.", task_id=task_id)
        return updated

    # -- approvals ----------------------------------------------------------------------

    def request_approval(self, task: ServerTask, query: Any, *,
                         control: Any = None, model: str = "",
                         provider: str = "") -> str:
        """Worker-side approval bridge (blocks until decided).

        Flips the task to ``waiting_for_approval`` while the operator
        decides, mints the A33 token on approval, and restores the
        previous status afterwards. Returns ``""`` on denial/expiry
        (fail closed); raises :class:`TaskCancelled` on cancellation.
        """
        from forge.core.run_control import TaskCancelled

        current = self.tasks.get(task.task_id)
        if current is None or current.terminal:
            return ""
        if control is not None and control.cancel_requested:
            raise TaskCancelled("cancelled before approval")
        previous = current.status
        # File (and persist) the approval *before* flipping the status, so
        # a client that observes ``waiting_for_approval`` is guaranteed to
        # see the pending approval rows already queryable.
        try:
            filed = self.approvals.file_query(
                current, query, model=model, provider=provider)
        except (ServerError, ValueError) as exc:
            self.log(task.task_id, task.project_id,
                     "Could not file approval: %s" % exc,
                     level="error", source="approvals")
            return ""
        if previous == TaskStatus.RUNNING:
            try:
                self.tasks.transition(
                    task.task_id, TaskStatus.WAITING_FOR_APPROVAL,
                    expected=(TaskStatus.RUNNING,))
            except (InvalidTransition, Conflict):
                # Concurrent state change (cancellation/pause): leave the
                # decision to the terminal cleanup, which expires the
                # just-filed approval.
                return ""
        try:
            return self.approvals.wait_for_token(
                filed, control=control,
                fingerprint=getattr(query, "fingerprint", "") or "")
        finally:
            self._restore_running(task.task_id)

    def _restore_running(self, task_id: str) -> None:
        current = self.tasks.get(task_id)
        if (current is not None
                and current.status == TaskStatus.WAITING_FOR_APPROVAL):
            try:
                self.tasks.transition(
                    task_id, TaskStatus.RUNNING,
                    expected=(TaskStatus.WAITING_FOR_APPROVAL,))
            except (InvalidTransition, Conflict):
                pass

    def decide_approval(self, approval_id: str, approved: bool,
                        decided_by: str) -> Dict[str, Any]:
        record, duplicate = self.approvals.decide(
            approval_id, approved, decided_by)
        self.audit.record_decision(
            agent="forge-server", resource="approval",
            operation="decide", decision="ALLOW" if approved else "DENY",
            task_id=record["task_id"],
            reason="operator %s decided %s"
                   % (decided_by, "approve" if approved else "deny"),
            approval_id=approval_id)
        if duplicate:
            record = dict(record, duplicate=True)
        return record

    # -- views --------------------------------------------------------------------------------

    def recovery_bundle(self, *, after: int = 0,
                        since: Optional[float] = None,
                        project_id: str = "") -> Dict[str, Any]:
        return self.recovery.bundle(after=after, since=since,
                                    project_id=project_id)

    def status(self) -> Dict[str, Any]:
        counts = self.tasks.status_counts()
        return {
            "server": "forge-server",
            "version": _server_version(),
            "protocol_version": PROTOCOL_VERSION,
            "boot_id": self.boot_id,
            "running": self.running,
            "started_at": self.started_at,
            "uptime_seconds": (time.time() - self.started_at
                               if self.started_at else 0.0),
            "profile": self.config.profile,
            "resource_profile": self.governor.profile.to_dict(),
            "workers": {
                "max": self.pool.max_workers,
                "busy": self.pool.busy,
                "alive": self.pool.alive,
            },
            "scheduler": self.scheduler.stats(),
            "queue": {
                "depth": self.queue.depth(),
                "leased": self.queue.leased_count(),
            },
            "tasks": counts,
            "projects": len(self.projects.list()),
            "active_sessions": self.sessions.count_active(),
            "pending_approvals": len(self.approvals.pending()),
            "unread_notifications": self.notifications.unread_count(),
        }

    def health(self) -> Dict[str, Any]:
        return self.health_monitor.health()

    def authenticate(self, token: Any) -> Any:
        return self.auth.authenticate(token)

    # -- app / runner -------------------------------------------------------------------------

    def create_app(self) -> Any:
        from forge.server.api import create_app

        return create_app(self)

    def run_uvicorn(self, host: Optional[str] = None,
                    port: Optional[int] = None,
                    log_level: str = "info") -> None:  # pragma: no cover
        import uvicorn

        self.start()
        app = self.create_app()
        host = host or self.config.host
        port = int(port or self.config.port)
        print("Forge Server (A81): standalone task backend")
        print("  endpoint:  http://%s:%d/api/v1" % (host, port))
        print("  database:  %s" % self.config.db_path)
        print("  profile:   %s (A33 permission platform)"
              % self.config.profile)
        if self.config.local_dev_mode:
            print("  auth:      local-dev (API keys/sessions; no "
                  "passwords). Do not expose to untrusted networks.")
        uvicorn.run(app, host=host, port=port, log_level=log_level)


def _server_version() -> str:
    try:
        from forge.server import SERVER_VERSION

        return SERVER_VERSION
    except Exception:
        return "0.0.0"
