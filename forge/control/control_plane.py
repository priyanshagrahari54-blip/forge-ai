"""Central secure control plane for the Forge browser cockpit (A34).

:class:`ControlPlane` is the single backend authority behind the cockpit
API. It owns:

- persistent, project-scoped sessions (:mod:`forge.control.sessions`);
- task submission into the EXISTING :class:`PersistentTaskQueue
  <forge.core.task_queue.PersistentTaskQueue>` / :class:`TaskStore
  <forge.core.task_store.TaskStore>`;
- a background dispatcher + worker pool that runs tasks through the
  EXISTING :class:`Supervisor <forge.core.supervisor.Supervisor>` —
  never inline in an HTTP request, never tied to a browser tab;
- a persistent, replayable event log (:mod:`forge.control.events`);
- approval adaptation over the EXISTING A33
  :class:`ApprovalStore <forge.security.approvals.ApprovalStore>`
  (:mod:`forge.control.approvals`);
- pre-run checkpoints via the EXISTING
  :class:`CheckpointManager <forge.tools.checkpoint.CheckpointManager>`
  and candidate-scoped rollback through the EXISTING policy gate;
- read-only git/model/permission/verification views over existing
  subsystems;
- append-oriented audit via the EXISTING
  :class:`AuditLog <forge.security.audit.AuditLog>` with a JSONL sink.
- an A35 Desktop Bridge + Desktop Agent (``forge.desktop``): controlled
  desktop execution through the existing A33 policy/approval/audit
  systems, backed by the deterministic fake desktop provider.

The control plane implements NO policy of its own: every authorization
decision is delegated to A33. Unknown ids, cross-project access, and
unexpected states all fail closed.
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from forge.control.approvals import (
    ApprovalConflict,
    ApprovalError,
    ApprovalExpired,
    ApprovalForbidden,
    ApprovalNotFound,
    ApprovalService,
)
from forge.control.commands import (
    ControlCommand,
    Interpretation,
    InterpretedKind,
    interpret_text,
    interpret_voice_intent,
)
from forge.control.db import Database
from forge.control.events import EventStore
from forge.control.sessions import Session, SessionStore
from forge.core.report import redact
from forge.core.run_control import SupervisorControl
from forge.core.task_engine import TaskStatus
from forge.core.task_queue import PersistentTaskQueue
from forge.core.task_recovery import RecoveryPolicy, TaskRecoveryEngine
from forge.core.task_store import TaskStore
from forge.security.approvals import (
    ApprovalRequest,
    ApprovalStatus,
    ApprovalStore,
)
from forge.security.audit import AuditLog
from forge.security.permissions import OperationMode, PermissionManager
from forge.security.policy import PermissionPolicy, Resource
from forge.security.policy_gate import PolicyDecision, PolicyGate


# -- errors ---------------------------------------------------------------

class ControlError(Exception):
    """Base control-plane error; the API layer maps these to responses."""

    code = "INTERNAL_ERROR"
    status = 500

    def __init__(self, message: str = "", **details: Any) -> None:
        super().__init__(message)
        self.details = details


class NotFound(ControlError):
    code = "NOT_FOUND"
    status = 404


class TaskNotFound(NotFound):
    code = "TASK_NOT_FOUND"


class ProjectNotFound(NotFound):
    code = "PROJECT_NOT_FOUND"


class ApprovalNotFoundError(NotFound):
    code = "APPROVAL_NOT_FOUND"


class Forbidden(ControlError):
    code = "FORBIDDEN"
    status = 403


class PolicyDenied(Forbidden):
    code = "POLICY_DENIED"


class Conflict(ControlError):
    code = "TASK_CONFLICT"
    status = 409


class ApprovalConflictError(Conflict):
    code = "APPROVAL_CONFLICT"


class NotRunning(Conflict):
    code = "TASK_NOT_RUNNING"


class InvalidRequest(ControlError):
    code = "INVALID_REQUEST"
    status = 400


class ApprovalRequired(ControlError):
    code = "APPROVAL_REQUIRED"
    status = 409


class ApprovalExpiredError(ControlError):
    code = "APPROVAL_EXPIRED"
    status = 410


class RollbackFailed(ControlError):
    code = "ROLLBACK_FAILED"
    status = 409


class ModelUnavailable(ControlError):
    code = "MODEL_UNAVAILABLE"
    status = 503


# -- runs -----------------------------------------------------------------

class RunStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ROLLED_BACK = "ROLLED_BACK"


TERMINAL_STATUSES = frozenset({
    RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED,
    RunStatus.ROLLED_BACK,
})

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")

MAX_REQUIREMENT_CHARS = 8000


def validate_id(value: Any, *, kind: str = "id") -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise InvalidRequest(f"Malformed {kind}.")
    return value


@dataclass
class Run:
    id: str
    project_id: str
    requirement: str
    status: RunStatus
    stage: str
    version: int
    mode: str
    actor: str
    created_at: float
    updated_at: float
    started_at: float | None = None
    finished_at: float | None = None
    supervisor_task_id: str = ""
    report_json: str = "{}"
    error: str = ""
    checkpoint_id: str = ""
    files_json: str = "[]"
    model: str = ""
    provider: str = ""
    attempts: int = 1
    retry_of: str = ""
    pause_requested: bool = False
    rollback: bool = False

    def files(self) -> list[str]:
        try:
            values = json.loads(self.files_json or "[]")
        except ValueError:
            return []
        return [str(value) for value in values] if isinstance(values, list) else []

    def report(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.report_json or "{}")
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def to_dict(self, *, include_report: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": self.id,
            "project_id": self.project_id,
            "requirement": self.requirement,
            "status": self.status.value,
            "stage": self.stage,
            "version": self.version,
            "mode": self.mode,
            "actor": self.actor,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "supervisor_task_id": self.supervisor_task_id,
            "error": self.error,
            "checkpoint_id": self.checkpoint_id,
            "files": self.files(),
            "model": self.model,
            "provider": self.provider,
            "attempts": self.attempts,
            "retry_of": self.retry_of,
            "pause_requested": self.pause_requested,
            "rollback": self.rollback,
        }
        if include_report:
            payload["report"] = self.report()
        return payload


class RunStore:
    """SQLite-backed run records with optimistic concurrency."""

    _COLUMNS = ("id", "project_id", "requirement", "status", "stage",
                "version", "mode", "actor", "created_at", "updated_at",
                "started_at", "finished_at", "supervisor_task_id",
                "report_json", "error", "checkpoint_id", "files_json",
                "model", "provider", "attempts", "retry_of",
                "pause_requested", "rollback")

    def __init__(self, db: Database) -> None:
        self._db = db
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                requirement TEXT NOT NULL,
                status TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                mode TEXT NOT NULL DEFAULT 'assisted',
                actor TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                supervisor_task_id TEXT NOT NULL DEFAULT '',
                report_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL DEFAULT '',
                files_json TEXT NOT NULL DEFAULT '[]',
                model TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 1,
                retry_of TEXT NOT NULL DEFAULT '',
                pause_requested INTEGER NOT NULL DEFAULT 0,
                rollback INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_project "
            "ON runs(project_id, created_at)"
        )

    def create(self, run: Run) -> Run:
        self._db.execute(
            "INSERT INTO runs (" + ", ".join(self._COLUMNS) + ") VALUES ("
            + ", ".join("?" * len(self._COLUMNS)) + ")",
            self._to_row(run))
        return run

    def _to_row(self, run: Run) -> tuple:
        return (
            run.id, run.project_id, run.requirement, run.status.value,
            run.stage, run.version, run.mode, run.actor, run.created_at,
            run.updated_at, run.started_at, run.finished_at,
            run.supervisor_task_id, run.report_json, run.error,
            run.checkpoint_id, run.files_json, run.model, run.provider,
            run.attempts, run.retry_of, int(run.pause_requested),
            int(run.rollback))

    def _from_row(self, row: Any) -> Run:
        return Run(
            id=row["id"], project_id=row["project_id"],
            requirement=row["requirement"],
            status=RunStatus(row["status"]), stage=row["stage"] or "",
            version=int(row["version"]), mode=row["mode"] or "assisted",
            actor=row["actor"] or "", created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            started_at=row["started_at"], finished_at=row["finished_at"],
            supervisor_task_id=row["supervisor_task_id"] or "",
            report_json=row["report_json"] or "{}",
            error=row["error"] or "",
            checkpoint_id=row["checkpoint_id"] or "",
            files_json=row["files_json"] or "[]",
            model=row["model"] or "", provider=row["provider"] or "",
            attempts=int(row["attempts"] or 1),
            retry_of=row["retry_of"] or "",
            pause_requested=bool(row["pause_requested"]),
            rollback=bool(row["rollback"]))

    def get(self, run_id: str) -> Run | None:
        row = self._db.query_one(
            "SELECT * FROM runs WHERE id = ?", (run_id,))
        return self._from_row(row) if row is not None else None

    def get_by_supervisor_task(self, supervisor_task_id: str) -> Run | None:
        if not supervisor_task_id:
            return None
        row = self._db.query_one(
            "SELECT * FROM runs WHERE supervisor_task_id = ?",
            (supervisor_task_id,))
        return self._from_row(row) if row is not None else None

    def list_for_project(self, project_id: str, *,
                         status: str = "", limit: int = 50,
                         offset: int = 0) -> tuple[list[Run], int]:
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        where = "project_id = ?"
        params: list[Any] = [project_id]
        if status:
            where += " AND status = ?"
            params.append(status)
        total_row = self._db.query_one(
            f"SELECT COUNT(*) AS n FROM runs WHERE {where}", tuple(params))
        total = int(total_row["n"]) if total_row else 0
        rows = self._db.query(
            f"SELECT * FROM runs WHERE {where} "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            tuple(params + [limit, offset]))
        return [self._from_row(row) for row in rows], total

    def status_counts(self, project_id: str) -> dict[str, int]:
        rows = self._db.query(
            "SELECT status, COUNT(*) AS n FROM runs "
            "WHERE project_id = ? GROUP BY status",
            (project_id,))
        return {str(row["status"]): int(row["n"]) for row in rows}

    def compare_and_set(self, run_id: str, expected_version: int,
                        **updates: Any) -> Run | None:
        """Apply updates iff the version matches; bumps version on success."""
        allowed = {name for name in self._COLUMNS
                   if name not in ("id", "version")}
        for name in updates:
            if name not in allowed:
                raise InvalidRequest(f"Unknown run field: {name}")
        assignments = ", ".join(f"{name} = ?" for name in updates)
        params: list[Any] = []
        for name in updates:
            value = updates[name]
            if isinstance(value, RunStatus):
                value = value.value
            if isinstance(value, bool):
                value = int(value)
            params.append(value)
        params.append(time.time())
        params.extend([run_id, expected_version])
        cursor = self._db.execute(
            f"UPDATE runs SET {assignments}, updated_at = ?, "
            "version = version + 1 WHERE id = ? AND version = ?",
            tuple(params))
        if cursor.rowcount == 0:
            return None
        return self.get(run_id)

    def mutate(self, run_id: str, **updates: Any) -> Run | None:
        """Unconditional update (worker-owned transitions); bumps version."""
        run = self.get(run_id)
        if run is None:
            return None
        return self.compare_and_set(run_id, run.version, **updates)


# -- supervisor event mapping -----------------------------------------------

#: Supervisor ``stage_started`` payloads mapped to cockpit display stages.
DISPLAY_STAGES = {
    "PLAN": "planning", "AGENTS": "planning", "MODEL": "planning",
    "CODE": "coding", "TEST": "testing", "DEBUG": "debugging",
    "REPAIR": "debugging", "RETEST": "testing", "REVIEW": "review",
    "SECURITY": "security", "BENCHMARK": "benchmark",
    "ACCEPTANCE": "acceptance", "CHECKPOINT": "checkpoint",
    "COMMIT": "commit", "COMPLETED": "completed", "ROLLBACK": "rollback",
}

#: Supervisor report events mapped to cockpit event types.
SUPERVISOR_EVENTS = {
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
    "commit": "git.commit",
    "rollback": "rollback.completed",
    "cancelled": "task.cancelled",
}


# -- projects ---------------------------------------------------------------

@dataclass(frozen=True)
class Project:
    id: str
    name: str
    root: str


_PROJECT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


# -- configuration ----------------------------------------------------------

@dataclass
class ControlConfig:
    db_path: str | Path = ".forge/cockpit.db"
    projects: dict[str, str] = field(default_factory=dict)
    approval_timeout: float = 600.0
    approval_token_ttl: float = 300.0
    max_events_per_task: int = 5000
    max_workers: int = 4
    max_runs_per_project: int = 1
    max_sessions: int = 200
    session_ttl: float = 12 * 3600
    audit_sink: str | Path | None = None
    policy: PermissionPolicy | None = None
    fabric: Any = None
    checkpoint_retention: int = 10
    local_dev_mode: bool = True
    # A35 desktop: the provider behind the Desktop Bridge. Only the
    # deterministic fake exists in A35; real providers arrive as plugins.
    desktop_provider: Any = None
    desktop_bridge_ttl: float = 3600.0

    @classmethod
    def from_env(cls, **overrides: Any) -> "ControlConfig":
        """Build config with ``FORGE_`` environment overrides (no secrets)."""
        import os

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
            db_path=os.environ.get("FORGE_DB_PATH", ".forge/cockpit.db"),
            approval_timeout=env_float("FORGE_APPROVAL_TIMEOUT", 600.0),
            max_events_per_task=env_int("FORGE_MAX_EVENTS", 5000),
            max_workers=env_int("FORGE_MAX_WORKERS", 4),
            max_sessions=env_int("FORGE_MAX_SESSIONS", 200),
            session_ttl=env_float("FORGE_SESSION_TTL", 12 * 3600),
            local_dev_mode=os.environ.get("FORGE_AUTH_MODE",
                                           "local-dev") != "production",
            desktop_bridge_ttl=env_float("FORGE_DESKTOP_TTL", 3600.0),
        )
        provider_name = os.environ.get("FORGE_DESKTOP_PROVIDER", "").strip()
        if provider_name and provider_name != "fake":
            # A35 ships exactly one provider; refuse unknown names honestly.
            raise ValueError(
                f"Unknown FORGE_DESKTOP_PROVIDER {provider_name!r}; "
                "A35 supports 'fake' only")
        for key, value in overrides.items():
            setattr(config, key, value)
        return config


# -- control plane ----------------------------------------------------------

class ControlPlane:
    """The single backend authority behind the cockpit API."""

    def __init__(self, config: ControlConfig | None = None) -> None:
        self.config = config or ControlConfig()
        self._db = Database(self.config.db_path)
        self.sessions = SessionStore(self._db)
        self.events = EventStore(
            self._db, max_events_per_task=self.config.max_events_per_task)
        self.runs = RunStore(self._db)
        self._init_checkpoints_table()
        sink = (Path(self.config.audit_sink) if self.config.audit_sink
                else Path(self.config.db_path).parent / "audit.jsonl")
        sink.parent.mkdir(parents=True, exist_ok=True)
        self.audit = AuditLog(sink_path=sink)
        self.approval_store = ApprovalStore()
        self.approvals = ApprovalService(
            self.approval_store, resolve_run=self._resolve_approval_run,
            emit=self._emit, approval_timeout=config.approval_timeout,
            token_ttl=config.approval_token_ttl)
        self.projects: dict[str, Project] = {}
        for project_id, root in (config.projects or {}).items():
            self.register_project(project_id, root)
        if config.fabric is not None:
            self.fabric = config.fabric
        else:
            from forge.models.fabric import ModelFabric
            self.fabric = ModelFabric.from_defaults()
        self.policy = config.policy
        # A35 Desktop Agent: controlled execution through the existing A33
        # policy/approval/audit systems. Provider defaults to the
        # deterministic fake (dev/tests); real providers arrive as plugins.
        from forge.desktop.agent import DesktopAgent, GrantScopeChecker
        from forge.desktop.bridge import DesktopBridge
        from forge.desktop.profiles import DesktopProfile
        from forge.desktop.provider import FakeDesktopProvider

        desktop_provider = (config.desktop_provider
                            if config.desktop_provider is not None
                            else FakeDesktopProvider())
        desktop_agent = DesktopAgent(
            desktop_provider, policy=self.policy,
            store=self.approval_store, audit=self.audit,
            profile=DesktopProfile(mode="assisted"),
            scope_checker=GrantScopeChecker(
                ttl=config.desktop_bridge_ttl))
        self.desktop_bridge = DesktopBridge(
            desktop_agent, ttl=config.desktop_bridge_ttl)
        self.desktop = desktop_agent
        self._queues: dict[str, PersistentTaskQueue] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._active: dict[str, int] = {}
        self._active_lock = threading.Lock()
        self._controls: dict[str, SupervisorControl] = {}
        self._controls_lock = threading.Lock()
        self._checkpoints: dict[str, Any] = {}
        self._checkpoints_lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._dispatcher: threading.Thread | None = None
        self._dispatch_event = threading.Event()
        self._stopping = threading.Event()
        self._run_extra: dict[str, dict[str, Any]] = {}

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Start the dispatcher and worker pool (idempotent)."""
        if self._executor is not None:
            return
        for project_id, project in self.projects.items():
            queue = PersistentTaskQueue(
                store=TaskStore(Path(project.root) / ".forge" / "tasks.db"))
            queue.load()
            self._queues[project_id] = queue
            self._locks.setdefault(project_id, threading.Lock())
            self._active.setdefault(project_id, 0)
        self._recover_interrupted()
        self._stopping.clear()
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, self.config.max_workers),
            thread_name_prefix="forge-run")
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="forge-dispatch", daemon=True)
        self._dispatcher.start()

    def stop(self, *, wait: bool = True) -> None:
        """Stop background work. In-flight runs keep their threads only."""
        self._stopping.set()
        self._dispatch_event.set()
        # The dispatcher always joins: it exits promptly on the set event,
        # and leaking it would double-dispatch after a later start().
        if self._dispatcher is not None:
            self._dispatcher.join(timeout=5.0)
            self._dispatcher = None
        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=True)
            self._executor = None

    @property
    def running(self) -> bool:
        return self._executor is not None

    def _recover_interrupted(self) -> None:
        """Fail closed on runs left active by a previous process."""
        for project_id, queue in self._queues.items():
            engine = TaskRecoveryEngine(
                queue.store, RecoveryPolicy(retry_failed=False))
            for task in engine.recover_interrupted():
                run = self.runs.get(task.id)
                if run is None or run.status in TERMINAL_STATUSES:
                    continue
                self.runs.mutate(
                    run.id, status=RunStatus.FAILED,
                    error="Interrupted by control-plane restart; "
                          "no automatic resume.",
                    finished_at=time.time())
                self._emit(run.id, run.project_id, "task.failed",
                           {"reason": "interrupted",
                            "detail": "Control plane restarted; the run did "
                                      "not resume automatically."})

    # -- projects ---------------------------------------------------------

    def register_project(self, project_id: str, root: str | Path) -> Project:
        if (_PROJECT_ID_RE.fullmatch(project_id) is None
                or not isinstance(project_id, str)):
            raise InvalidRequest(f"Invalid project id: {project_id!r}")
        resolved = Path(root).resolve()
        if not resolved.is_dir():
            raise InvalidRequest(
                f"Project root is not a directory: {project_id!r}")
        project = Project(id=project_id, name=resolved.name,
                          root=str(resolved))
        self.projects[project_id] = project
        return project

    def get_project(self, project_id: str) -> Project:
        try:
            return self.projects[project_id]
        except KeyError:
            raise ProjectNotFound(
                f"Unknown project: {project_id!r}") from None

    def list_projects(self) -> list[dict[str, Any]]:
        return [
            {"project_id": project.id, "name": project.name,
             "root": project.root}
            for project in self.projects.values()
        ]

    # -- sessions ---------------------------------------------------------

    def create_session(self, actor: str, project_id: str, *,
                       profile: str = "assisted") -> tuple[Session, str]:
        self.get_project(project_id)  # fail closed on unknown projects
        try:
            OperationMode(profile)
        except ValueError:
            raise InvalidRequest(
                f"Unknown profile: {profile!r}") from None
        self.sessions.prune()
        if self.sessions.count_active() >= max(1, self.config.max_sessions):
            raise Conflict("Too many active sessions.")
        try:
            session, token = self.sessions.create(
                actor, project_id, profile=profile,
                ttl_seconds=self.config.session_ttl)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(actor, "session", "create", True,
                    reason=f"project {project_id} profile {profile}")
        return session, token

    def revoke_session(self, session: Session) -> None:
        self.sessions.revoke(session.id)
        self._audit(session.actor, "session", "revoke", True,
                    reason=f"session {session.id}")

    # -- tasks ------------------------------------------------------------

    def submit_task(self, session: Session, requirement: str, *,
                    mode: str = "") -> Run:
        if not isinstance(requirement, str) or not requirement.strip():
            raise InvalidRequest("Requirement must be a non-empty string.")
        requirement = requirement.strip()
        if len(requirement) > MAX_REQUIREMENT_CHARS:
            raise InvalidRequest("Requirement is too long.")
        profile = mode or session.profile
        try:
            OperationMode(profile)
        except ValueError:
            raise InvalidRequest(f"Unknown mode: {profile!r}") from None
        project = self.get_project(session.project_id)
        queue = self._queue_for(project.id)
        run_id = f"t-{uuid4().hex[:16]}"
        validate_id(run_id, kind="task id")
        queue.add(run_id, requirement)
        now = time.time()
        run = self.runs.create(Run(
            id=run_id, project_id=project.id, requirement=requirement,
            status=RunStatus.QUEUED, stage="queued", version=1,
            mode=profile, actor=session.actor, created_at=now,
            updated_at=now))
        self.sessions.set_active_task(session.id, run_id)
        self._emit(run.id, run.project_id, "task.created",
                   {"requirement_chars": len(requirement),
                    "mode": profile, "actor": session.actor})
        self._emit(run.id, run.project_id, "task.queued", {})
        self._audit(session.actor, "task", "submit", True,
                    task_id=run.id, reason=f"project {project.id}")
        self._dispatch_event.set()
        return run

    def get_task(self, session: Session, task_id: str) -> Run:
        validate_id(task_id, kind="task id")
        run = self.runs.get(task_id)
        # Cross-project ids map to NOT_FOUND: existence must not leak.
        if run is None or run.project_id != session.project_id:
            raise TaskNotFound(f"Unknown task: {task_id!r}")
        return run

    def list_tasks(self, session: Session, *, status: str = "",
                   limit: int = 50, offset: int = 0) -> tuple[list[Run], int]:
        if status:
            try:
                RunStatus(status)
            except ValueError:
                raise InvalidRequest(
                    f"Unknown status: {status!r}") from None
        return self.runs.list_for_project(
            session.project_id, status=status, limit=limit, offset=offset)

    def _check_version(self, run: Run,
                       expected_version: int | None) -> None:
        if expected_version is None:
            return
        if int(expected_version) != run.version:
            raise Conflict(
                "Stale task version; refresh and retry.",
                expected=expected_version, actual=run.version)

    def pause_task(self, session: Session, task_id: str, *,
                   expected_version: int | None = None) -> Run:
        run = self.get_task(session, task_id)
        self._check_version(run, expected_version)
        if run.status == RunStatus.QUEUED:
            updated = self.runs.compare_and_set(
                run.id, run.version, status=RunStatus.PAUSED)
            if updated is None:
                raise Conflict("Task changed concurrently; retry.")
            self._emit(run.id, run.project_id, "task.paused",
                       {"detail": "Paused while queued."})
            self._audit(session.actor, "task", "pause", True, task_id=run.id)
            return updated
        if run.status in (RunStatus.RUNNING, RunStatus.WAITING_APPROVAL):
            control = self._control_for(run.id)
            if control is None:
                raise Conflict("Task is not under active control.")
            control.request_pause()
            updated = self.runs.compare_and_set(
                run.id, run.version, pause_requested=True)
            if updated is None:
                raise Conflict("Task changed concurrently; retry.")
            self._audit(session.actor, "task", "pause", True, task_id=run.id,
                        reason="pause requested; takes effect at the next "
                               "stage boundary")
            return updated
        if run.status in TERMINAL_STATUSES:
            raise NotRunning(f"Cannot pause a {run.status.value} task.")
        raise Conflict(f"Cannot pause a {run.status.value} task.")

    def resume_task(self, session: Session, task_id: str, *,
                    expected_version: int | None = None) -> Run:
        run = self.get_task(session, task_id)
        self._check_version(run, expected_version)
        if run.status != RunStatus.PAUSED:
            if run.status in TERMINAL_STATUSES:
                raise NotRunning(
                    f"Cannot resume a {run.status.value} task.")
            raise Conflict(
                f"Cannot resume a {run.status.value} task.")
        if run.started_at is None:
            updated = self.runs.compare_and_set(
                run.id, run.version, status=RunStatus.QUEUED,
                pause_requested=False)
            if updated is None:
                raise Conflict("Task changed concurrently; retry.")
            self._emit(run.id, run.project_id, "task.resumed",
                       {"detail": "Resumed while queued."})
            self._dispatch_event.set()
            self._audit(session.actor, "task", "resume", True,
                        task_id=run.id)
            return updated
        control = self._control_for(run.id)
        if control is None:
            raise Conflict("Task is not under active control.")
        control.request_resume()
        updated = self.runs.compare_and_set(
            run.id, run.version, pause_requested=False)
        if updated is None:
            raise Conflict("Task changed concurrently; retry.")
        self._audit(session.actor, "task", "resume", True, task_id=run.id)
        return updated

    def cancel_task(self, session: Session, task_id: str, *,
                    expected_version: int | None = None) -> Run:
        run = self.get_task(session, task_id)
        self._check_version(run, expected_version)
        if run.status == RunStatus.QUEUED or (
                run.status == RunStatus.PAUSED and run.started_at is None):
            updated = self.runs.compare_and_set(
                run.id, run.version, status=RunStatus.CANCELLED,
                finished_at=time.time(), error="Cancelled before start.")
            if updated is None:
                raise Conflict("Task changed concurrently; retry.")
            self._mirror_fail(run, "Cancelled before start.")
            self._emit(run.id, run.project_id, "task.cancelled",
                       {"detail": "Cancelled before start."})
            self._audit(session.actor, "task", "cancel", True,
                        task_id=run.id)
            return updated
        if run.status in (RunStatus.RUNNING, RunStatus.PAUSED,
                          RunStatus.WAITING_APPROVAL):
            control = self._control_for(run.id)
            if control is None:
                raise Conflict("Task is not under active control.")
            control.request_cancel()
            self._emit(run.id, run.project_id, "task.cancel_requested", {})
            self._audit(session.actor, "task", "cancel", True,
                        task_id=run.id,
                        reason="cancel requested; takes effect at the next "
                               "stage boundary")
            return run
        if run.status in TERMINAL_STATUSES:
            raise NotRunning(f"Cannot cancel a {run.status.value} task.")
        raise Conflict(f"Cannot cancel a {run.status.value} task.")

    def retry_task(self, session: Session, task_id: str) -> Run:
        run = self.get_task(session, task_id)
        if run.status not in TERMINAL_STATUSES:
            raise Conflict("Only finished tasks can be retried.")
        project = self.get_project(session.project_id)
        queue = self._queue_for(project.id)
        run_id = f"t-{uuid4().hex[:16]}"
        queue.add(run_id, run.requirement)
        now = time.time()
        retried = self.runs.create(Run(
            id=run_id, project_id=project.id,
            requirement=run.requirement, status=RunStatus.QUEUED,
            stage="queued", version=1, mode=run.mode,
            actor=session.actor, created_at=now, updated_at=now,
            attempts=run.attempts + 1, retry_of=run.id))
        self.sessions.set_active_task(session.id, run_id)
        self._emit(retried.id, retried.project_id, "task.created",
                   {"retry_of": run.id, "attempts": retried.attempts,
                    "mode": retried.mode, "actor": session.actor})
        self._emit(retried.id, retried.project_id, "task.queued", {})
        self._audit(session.actor, "task", "retry", True, task_id=retried.id,
                    reason=f"retry of {run.id}")
        self._dispatch_event.set()
        return retried

    # -- task views -------------------------------------------------------

    def get_task_events(self, session: Session, task_id: str, *,
                        after: int = 0, limit: int = 200):
        run = self.get_task(session, task_id)
        events, latest = self.events.list(run.id, after=after, limit=limit)
        return [event.to_dict() for event in events], latest

    def wait_task_events(self, session: Session, task_id: str, *,
                         after: int = 0, timeout: float = 25.0):
        run = self.get_task(session, task_id)
        events = self.events.wait(
            run.id, after, timeout=max(1.0, min(30.0, timeout)))
        return [event.to_dict() for event in events]

    def get_task_report(self, session: Session, task_id: str) -> dict[str, Any]:
        run = self.get_task(session, task_id)
        return {
            "task_id": run.id, "status": run.status.value,
            "stage": run.stage, "mode": run.mode,
            "model": run.model, "provider": run.provider,
            "files": run.files(), "error": run.error,
            "rollback": run.rollback,
            "report": redact(run.report()),
        }

    def get_task_logs(self, session: Session, task_id: str) -> dict[str, Any]:
        run = self.get_task(session, task_id)
        report = run.report()
        return {
            "task_id": run.id,
            "stages": report.get("stages", []),
            "error": report.get("error", ""),
            "rollback": report.get("rollback", False),
            "retries": report.get("retries", 0),
            "timings": report.get("timings", {}),
            "acceptance": report.get("acceptance", {}),
        }

    def get_verification(self, session: Session,
                         task_id: str) -> dict[str, Any]:
        run = self.get_task(session, task_id)
        report = run.report()
        return {
            "task_id": run.id,
            "status": run.status.value,
            "tests": report.get("test_result", {}),
            "review": report.get("review_result", {}),
            "security": report.get("security_result", {}),
            "build": report.get("build_result", {}),
            "acceptance": report.get("acceptance", {}),
            "benchmark": report.get("benchmark", {}),
        }

    # -- approvals ----------------------------------------------------------

    def _resolve_approval_run(self, task_id: str) -> Run | None:
        if not task_id:
            return None
        run = self.runs.get(task_id)
        if run is not None:
            return run
        return self.runs.get_by_supervisor_task(task_id)

    def list_approvals(self, session: Session) -> list[dict[str, Any]]:
        requests = self.approvals.pending_for_project(session.project_id)
        return [self._approval_to_dict(request) for request in requests]

    def get_approval(self, session: Session,
                     approval_id: str) -> dict[str, Any]:
        validate_id(approval_id, kind="approval id")
        request = self.approvals.get(approval_id)
        if request is None:
            raise ApprovalNotFoundError(
                f"Unknown approval: {approval_id!r}")
        run = self._resolve_approval_run(request.task_id)
        if run is None or run.project_id != session.project_id:
            raise ApprovalNotFoundError(
                f"Unknown approval: {approval_id!r}")
        return self._approval_to_dict(request)

    def _approval_to_dict(self, request: ApprovalRequest) -> dict[str, Any]:
        run = self._resolve_approval_run(request.task_id)
        return {
            **request.to_dict(),
            "task_ref": run.id if run is not None else "",
            "project_id": run.project_id if run is not None else "",
        }

    def approve_request(self, session: Session, approval_id: str) -> dict[str, Any]:
        return self._decide(session, approval_id, True)

    def deny_request(self, session: Session, approval_id: str) -> dict[str, Any]:
        return self._decide(session, approval_id, False)

    def _decide(self, session: Session, approval_id: str,
                approved: bool) -> dict[str, Any]:
        validate_id(approval_id, kind="approval id")
        # Visibility pre-check: cross-project ids are NOT_FOUND, never 403.
        request = self.approvals.get(approval_id)
        if request is None:
            raise ApprovalNotFoundError(
                f"Unknown approval: {approval_id!r}")
        run = self._resolve_approval_run(request.task_id)
        if run is None or run.project_id != session.project_id:
            raise ApprovalNotFoundError(
                f"Unknown approval: {approval_id!r}")
        try:
            decided, duplicate = self.approvals.decide(
                approval_id, approved, session.actor,
                project_id=session.project_id)
        except ApprovalNotFound as exc:
            raise ApprovalNotFoundError(str(exc)) from exc
        except ApprovalForbidden as exc:
            raise Forbidden(str(exc)) from exc
        except ApprovalConflict as exc:
            raise ApprovalConflictError(str(exc)) from exc
        except ApprovalExpired as exc:
            raise ApprovalExpiredError(str(exc)) from exc
        self._audit(session.actor, "approval",
                    "approve" if approved else "deny", True,
                    task_id=run.id,
                    reason=f"approval {approval_id} "
                           f"({'duplicate' if duplicate else 'decided'})")
        payload = self._approval_to_dict(decided)
        payload["duplicate"] = duplicate
        return payload

    # -- checkpoints & rollback -----------------------------------------------

    def _init_checkpoints_table(self) -> None:
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                snapshot_path TEXT NOT NULL DEFAULT '',
                paths_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'available'
            )
            """
        )

    def list_checkpoints(self, session: Session,
                         task_id: str) -> list[dict[str, Any]]:
        run = self.get_task(session, task_id)
        rows = self._db.query(
            "SELECT * FROM checkpoints WHERE run_id = ? "
            "ORDER BY created_at DESC",
            (run.id,))
        with self._checkpoints_lock:
            live = set(self._checkpoints)
        result = []
        for row in rows:
            try:
                paths = json.loads(row["paths_json"] or "[]")
            except ValueError:
                paths = []
            status = row["status"]
            if status == "available" and row["id"] not in live:
                status = "unavailable"
            result.append({
                "checkpoint_id": row["id"],
                "task_id": row["run_id"],
                "created_at": row["created_at"],
                "label": row["label"] or "",
                "affected_paths": paths,
                "status": status,
            })
        return result

    def _register_checkpoint(self, run: Run, checkpoint: Any,
                             label: str) -> None:
        try:
            paths = sorted(checkpoint.files or {})
        except Exception:
            paths = []
        # Metadata stores a bounded preview; the snapshot holds the bytes.
        preview = paths[:500]
        self._db.execute(
            "INSERT OR REPLACE INTO checkpoints (id, run_id, project_id, "
            "created_at, label, snapshot_path, paths_json, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'available')",
            (checkpoint.id, run.id, run.project_id, time.time(), label,
             str(getattr(checkpoint, "snapshot", "")),
             json.dumps(preview)))
        with self._checkpoints_lock:
            self._checkpoints[checkpoint.id] = checkpoint
            while len(self._checkpoints) > max(
                    1, self.config.checkpoint_retention):
                oldest = next(iter(self._checkpoints))
                stale = self._checkpoints.pop(oldest)
                try:
                    from forge.tools.checkpoint import CheckpointManager
                    CheckpointManager(
                        self.get_project(run.project_id).root).cleanup(stale)
                except Exception:
                    pass
                self._db.execute(
                    "UPDATE checkpoints SET status = 'evicted' WHERE id = ?",
                    (oldest,))

    def rollback_task(self, session: Session, task_id: str, *,
                      checkpoint_id: str = "",
                      expected_version: int | None = None) -> dict[str, Any]:
        run = self.get_task(session, task_id)
        self._check_version(run, expected_version)
        if run.status != RunStatus.SUCCEEDED:
            raise RollbackFailed(
                "Only completed tasks can be rolled back. "
                "Failed runs already restore their candidate files; "
                "cancelled or rolled-back tasks have nothing to restore.")
        files = run.files()
        if not files:
            raise RollbackFailed("No candidate files were recorded.")
        row = None
        if checkpoint_id:
            validate_id(checkpoint_id, kind="checkpoint id")
            row = self._db.query_one(
                "SELECT * FROM checkpoints WHERE id = ? AND run_id = ?",
                (checkpoint_id, run.id))
            if row is None:
                raise NotFound("Unknown checkpoint for this task.")
        else:
            row = self._db.query_one(
                "SELECT * FROM checkpoints WHERE run_id = ? "
                "ORDER BY created_at DESC",
                (run.id,))
            if row is None:
                raise RollbackFailed(
                    "No checkpoint is recorded for this task.")
        with self._checkpoints_lock:
            checkpoint = self._checkpoints.get(row["id"])
        if checkpoint is None or row["status"] != "available":
            raise RollbackFailed(
                "Checkpoint is unavailable (evicted or control plane "
                "restarted).")
        self._emit(run.id, run.project_id, "rollback.requested",
                   {"checkpoint_id": row["id"], "files": files,
                    "actor": session.actor})
        self._audit(session.actor, "rollback", "request", True,
                    task_id=run.id,
                    reason=f"checkpoint {row['id']} files {len(files)}")
        # Authorize every restored file through the existing gate.
        project = self.get_project(run.project_id)
        permissions = PermissionManager(
            mode=OperationMode.ASSISTED, policy=self.policy,
            store=self.approval_store, agent="cockpit-rollback",
            audit=self.audit)
        gate = PolicyGate(permissions)
        token_id = self._rollback_token(run, files, checkpoint_id=row["id"])
        if token_id is None:
            pending = self._pending_rollback_request(run, files)
            if pending is None:
                pending = self.approvals.file_rollback(
                    run, files, checkpoint_id=row["id"])
            raise ApprovalRequired(
                "Rollback requires approval.",
                approval_id=pending.id)
        for path in files:
            outcome = gate.evaluate(
                operation="write_file", path=path, tool="write_file",
                risk="MEDIUM", capability="rollback", approved=False,
                agent="cockpit-rollback", task_id=run.id,
                approval_token_id=token_id, fingerprint=row["id"])
            if not outcome.allowed:
                raise PolicyDenied(
                    f"Rollback of {path} denied: {outcome.reason}")
        from forge.tools.checkpoint import CheckpointManager
        try:
            CheckpointManager(project.root).rollback(checkpoint, files)
        except Exception as exc:
            raise RollbackFailed(f"Rollback failed: {exc}") from exc
        with self._checkpoints_lock:
            self._checkpoints.pop(row["id"], None)
        self._db.execute(
            "UPDATE checkpoints SET status = 'consumed' WHERE id = ?",
            (row["id"],))
        updated = self.runs.mutate(
            run.id, status=RunStatus.ROLLED_BACK, rollback=True)
        if updated is None:  # pragma: no cover - defensive
            raise Conflict("Task changed concurrently; retry.")
        self.approval_store.revoke_task(run.id)
        self._emit(run.id, run.project_id, "rollback.completed",
                   {"checkpoint_id": row["id"], "files": files,
                    "untouched": "All files outside the candidate set were "
                                 "left untouched."})
        self._audit(session.actor, "rollback", "complete", True,
                    task_id=run.id,
                    reason=f"checkpoint {row['id']}")
        return {"task_id": run.id, "status": updated.status.value,
                "checkpoint_id": row["id"], "files": files}

    def _pending_rollback_request(self, run: Run,
                                  files: list[str]) -> Any | None:
        for request in self.approval_store.all_requests():
            if (request.task_id == run.id
                    and request.agent == "cockpit-rollback"
                    and list(request.files) == files
                    and request.status == ApprovalStatus.PENDING
                    and not request.expired(self.approval_store.now())):
                return request
        return None

    def _rollback_token(self, run: Run, files: list[str], *,
                        checkpoint_id: str) -> str | None:
        """Mint a token from an approved rollback request, if present."""
        for request in self.approval_store.all_requests():
            if (request.task_id == run.id
                    and request.agent == "cockpit-rollback"
                    and list(request.files) == files
                    and request.status == ApprovalStatus.APPROVED
                    and not request.expired(self.approval_store.now())):
                try:
                    token = self.approval_store.issue(
                        request.id, request.decided_by,
                        ttl_seconds=self.config.approval_token_ttl,
                        max_uses=max(1, len(files)),
                        fingerprint=checkpoint_id)
                except ValueError:
                    return None
                return token.id
        return None

    # -- git views (read-only) --------------------------------------------------

    def git_status(self, session: Session) -> dict[str, Any]:
        from forge.tools.git import GitTool

        project = self.get_project(session.project_id)
        git = GitTool(project.root)
        try:
            branch_proc = git.run("branch", "--show-current")
            head_proc = git.run("rev-parse", "--short", "HEAD")
            if branch_proc.returncode != 0 or head_proc.returncode != 0:
                raise RuntimeError("not a git repository")
            branch = branch_proc.stdout.strip()
            head = head_proc.stdout.strip()
        except Exception:
            return {"project_id": project.id, "available": False,
                    "reason": "Not a git repository."}
        try:
            status = git.status()
            changed = git.changed_files()
            log = git.run("log", "-5", "--oneline").stdout.strip()
        except Exception as exc:
            return {"project_id": project.id, "available": True,
                    "branch": branch, "head": head,
                    "reason": f"git state unavailable: {exc}"}
        return {"project_id": project.id, "available": True,
                "branch": branch or "(detached)", "head": head,
                "working_tree": status,
                "changed_files": changed[:200],
                "recent_commits": log.splitlines()[:5]}

    def git_diff(self, session: Session, *,
                 staged: bool = False) -> dict[str, Any]:
        from forge.tools.git import GitTool

        project = self.get_project(session.project_id)
        git = GitTool(project.root)
        try:
            diff = git.diff(staged=staged)
        except Exception as exc:
            raise InvalidRequest(f"Diff unavailable: {exc}") from exc
        truncated = len(diff) > 200_000
        return {"project_id": project.id, "staged": staged,
                "diff": diff[:200_000], "truncated": truncated}

    # -- model views (read-only) --------------------------------------------------

    def get_model_state(self) -> dict[str, Any]:
        fabric = self.fabric
        try:
            models = [model.to_dict() for model in fabric.models()]
        except Exception:
            models = []
        try:
            health = fabric.health()
        except Exception:
            health = {}
        try:
            provider_health = fabric.provider_health()
        except Exception:
            provider_health = {}
        try:
            providers = fabric.providers.snapshot()
        except Exception:
            providers = []
        try:
            policy = fabric.policy.to_dict()
        except Exception:
            policy = {}
        try:
            history = list(fabric.router.history[-20:])
        except Exception:
            history = []
        return redact({
            "models": models, "health": health,
            "provider_health": provider_health, "providers": providers,
            "routing_policy": policy, "recent_routing": history,
        })

    def model_health(self) -> dict[str, Any]:
        state = self.get_model_state()
        return {"models": state["health"],
                "providers": state["provider_health"]}

    # -- permission views (read-only) -----------------------------------------------

    def get_permission_state(self, session: Session) -> dict[str, Any]:
        operations = ("read_file", "search_files", "write_file",
                      "delete_file", "run_command", "run_tests",
                      "git_status", "git_diff", "git_commit", "git_push")
        manager = PermissionManager(mode=OperationMode(session.profile))
        effective = {}
        for operation in operations:
            try:
                effective[operation] = manager.check(operation).value
            except Exception:
                effective[operation] = "unknown"
        rules: list[dict[str, Any]] = []
        if self.policy is not None:
            try:
                rules = [rule.to_dict() for rule in self.policy.rules]
            except Exception:
                rules = []
        pending = self.list_approvals(session)
        return {"project_id": session.project_id,
                "profile": session.profile,
                "actor": session.actor,
                "effective": effective,
                "policy_rules": rules,
                "pending_approvals": pending}

    # -- project state ----------------------------------------------------------

    def get_project_state(self, session: Session) -> dict[str, Any]:
        project = self.get_project(session.project_id)
        counts = self.runs.status_counts(project.id)
        recent, _ = self.runs.list_for_project(project.id, limit=5)
        running = self.runs.list_for_project(
            project.id, status=RunStatus.RUNNING.value, limit=1)[0]
        git = self.git_status(session)
        return {"project_id": project.id, "name": project.name,
                "root": project.root,
                "counts": counts,
                "active_task": (running[0].to_dict() if running else None),
                "recent_runs": [run.to_dict() for run in recent],
                "git": git}

    def dashboard(self, session: Session) -> dict[str, Any]:
        project = self.get_project(session.project_id)
        counts = self.runs.status_counts(project.id)
        recent_events = self.events.recent_for_project(project.id, limit=20)
        health = self.model_health()
        models = health["models"] if isinstance(health, dict) else {}
        healthy = sum(1 for state in models.values()
                      if isinstance(state, dict)
                      and state.get("health") == "healthy")
        degraded = sum(1 for state in models.values()
                       if isinstance(state, dict)
                       and state.get("health") == "degraded")
        unavailable = sum(1 for state in models.values()
                          if isinstance(state, dict)
                          and state.get("health") not in
                          ("healthy", "degraded"))
        with self._active_lock:
            busy = int(self._active.get(project.id, 0))
        return {
            "project_id": project.id,
            "tasks": {
                "queued": counts.get("QUEUED", 0),
                "running": counts.get("RUNNING", 0),
                "waiting_approval": counts.get("WAITING_APPROVAL", 0),
                "paused": counts.get("PAUSED", 0),
                "succeeded": counts.get("SUCCEEDED", 0),
                "failed": counts.get("FAILED", 0),
                "cancelled": counts.get("CANCELLED", 0),
                "rolled_back": counts.get("ROLLED_BACK", 0),
            },
            "approvals_waiting": len(self.list_approvals(session)),
            "models": {"healthy": healthy, "degraded": degraded,
                       "unavailable": unavailable},
            "workers": {"busy": busy,
                        "max_per_project": self.config.max_runs_per_project,
                        "running": self.running},
            "recent_activity": [event.to_dict()
                                for event in recent_events],
        }

    # -- recovery ---------------------------------------------------------------

    def request_recovery(self, session: Session) -> dict[str, Any]:
        queue = self._queue_for(session.project_id)
        engine = TaskRecoveryEngine(
            queue.store, RecoveryPolicy(retry_failed=False))
        interrupted = engine.recover_interrupted()
        recovered_runs: list[str] = []
        for task in interrupted:
            run = self.runs.get(task.id)
            if run is None or run.status in TERMINAL_STATUSES:
                continue
            self.runs.mutate(
                run.id, status=RunStatus.FAILED,
                error="Marked failed by recovery: run was interrupted.",
                finished_at=time.time())
            recovered_runs.append(run.id)
        self._audit(session.actor, "recovery", "request", True,
                    reason=f"project {session.project_id}: "
                           f"{len(recovered_runs)} interrupted run(s)")
        return {"project_id": session.project_id,
                "interrupted_store_tasks": [task.id for task in interrupted],
                "failed_runs": recovered_runs}

    # -- commands / voice / desktop foundations -----------------------------------

    def execute_command(self, session: Session, command: str, *,
                        task_id: str = "", approval_id: str = "",
                        requirement: str = "",
                        expected_version: int | None = None) -> dict[str, Any]:
        try:
            action = ControlCommand(command)
        except ValueError:
            raise InvalidRequest(
                f"Unknown command: {command!r}") from None
        if action == ControlCommand.START_TASK:
            run = self.submit_task(session, requirement)
            return {"command": action.value, "task": run.to_dict()}
        if action in (ControlCommand.PAUSE_TASK, ControlCommand.RESUME_TASK,
                      ControlCommand.CANCEL_TASK):
            if not task_id:
                raise InvalidRequest("task_id is required.")
            handler = {
                ControlCommand.PAUSE_TASK: self.pause_task,
                ControlCommand.RESUME_TASK: self.resume_task,
                ControlCommand.CANCEL_TASK: self.cancel_task,
            }[action]
            run = handler(session, task_id,
                          expected_version=expected_version)
            return {"command": action.value, "task": run.to_dict()}
        if action == ControlCommand.RETRY_TASK:
            if not task_id:
                raise InvalidRequest("task_id is required.")
            run = self.retry_task(session, task_id)
            return {"command": action.value, "task": run.to_dict()}
        if action == ControlCommand.ROLLBACK:
            if not task_id:
                raise InvalidRequest("task_id is required.")
            try:
                result = self.rollback_task(
                    session, task_id, expected_version=expected_version)
            except ApprovalRequired as exc:
                return {"command": action.value,
                        "approval_required": True,
                        "approval_id": exc.details.get("approval_id")}
            return {"command": action.value, **result}
        if action in (ControlCommand.APPROVE, ControlCommand.DENY):
            if not approval_id:
                raise InvalidRequest("approval_id is required.")
            if action == ControlCommand.APPROVE:
                payload = self.approve_request(session, approval_id)
            else:
                payload = self.deny_request(session, approval_id)
            return {"command": action.value, "approval": payload}
        raise InvalidRequest(f"Unsupported command: {command!r}")

    def interpret(self, session: Session, text: str) -> dict[str, Any]:
        pending = self.list_approvals(session)
        interpretation = interpret_text(
            text, active_task=session.active_task,
            pending_approval=pending[0]["id"] if pending else "")
        return {"interpretation": interpretation.to_dict(),
                "executed": False}

    def voice_interpret(self, session: Session,
                        text: str) -> dict[str, Any]:
        from forge.voice import VoiceCommand, VoiceInterface

        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise InvalidRequest("Voice text must be 1-2000 characters.")
        voice = VoiceInterface(
            policy=self.policy, store=self.approval_store,
            audit=self.audit)
        command = VoiceCommand(text=text.strip(), agent=session.actor,
                               task_id=session.active_task)
        intent, permission = voice.check(command)
        pending = self.list_approvals(session)
        interpretation = interpret_voice_intent(
            intent.name, dict(intent.slots),
            active_task=session.active_task,
            pending_approval=pending[0]["id"] if pending else "")
        if interpretation.kind == InterpretedKind.COMMAND:
            # Voice translation is advisory in A34: nothing executes.
            interpretation = Interpretation(
                InterpretedKind.REQUIRE_CLARIFICATION,
                interpretation.command,
                dict(interpretation.slots),
                reason="Voice execution is not enabled in A34; confirm in "
                       "the cockpit to run this command.")
        return {"intent": {"name": intent.name,
                           "slots": dict(intent.slots),
                           "confidence": intent.confidence},
                "permission": permission.to_dict(),
                "interpretation": interpretation.to_dict(),
                "executed": False}

    # -- desktop agent (A35) -----------------------------------------------------

    def _desktop_profile(self, session: Session):
        from forge.desktop.profiles import profile_from_session

        return profile_from_session(session.profile)

    def _desktop_session(self, session: Session):
        """Get (or create) the bridge session for a cockpit session."""
        bridge_id = f"cockpit:{session.id}"
        try:
            return self.desktop_bridge.session(bridge_id)
        except Exception:
            return self.desktop_bridge.open_session(
                session.actor, session.project_id, bridge_id=bridge_id)

    def desktop_capabilities(self, session: Session) -> dict[str, Any]:
        """A35: honest capability matrix for the session's profile."""
        from forge.desktop.profiles import PROFILE_LABELS

        profile = self._desktop_profile(session)
        capabilities = self.desktop.available_actions(profile=profile)
        return {
            "status": "simulation",
            "note": "A35 controls the deterministic fake desktop through "
                    "the A33 permission system. No real OS resources are "
                    "touched; real providers arrive as plugins.",
            "provider": self.desktop_bridge.provider_kind(),
            "profile": profile.mode,
            "profile_label": PROFILE_LABELS.get(profile.mode, profile.mode),
            "capabilities": capabilities,
        }

    def desktop_check(self, session: Session, action: str,
                      target: str = "", params: dict[str, Any] | None = None,
                      task_id: str = "") -> dict[str, Any]:
        """Evaluate a desktop request through the A35 pipeline without
        executing anything."""
        from forge.desktop.actions import DesktopActionKind, DesktopRequest

        try:
            kind = DesktopActionKind(action)
        except ValueError:
            raise InvalidRequest(
                f"Unknown desktop action: {action!r}") from None
        if not isinstance(target, str) or len(target) > 500:
            raise InvalidRequest("Invalid desktop target.")
        request = DesktopRequest(
            kind, target=target, params=dict(params or {}),
            agent=session.actor, task_id=task_id or session.active_task,
            reason="cockpit desktop permission preview")
        authorization = self.desktop.authorize(
            request, profile=self._desktop_profile(session))
        self._audit(session.actor, "desktop", request.policy_operation(),
                    authorization.allowed,
                    reason=f"preview only: {authorization.reason}",
                    task_id=request.task_id)
        payload = authorization.to_dict()
        payload.update({"action": kind.value, "target": target,
                        "executed": False})
        return payload

    def desktop_state(self, session: Session) -> dict[str, Any]:
        """Observation snapshot of the desktop through the full pipeline."""
        from forge.desktop.actions import DesktopRequest

        profile = self._desktop_profile(session)
        observations: dict[str, Any] = {}
        probes = (
            ("screenshot", DesktopRequest("screenshot", agent="forge-desktop",
                                          task_id=session.active_task)),
            ("active_window", DesktopRequest("read_screen",
                                             agent="forge-desktop",
                                             task_id=session.active_task)),
            ("windows", DesktopRequest("window_list", agent="forge-desktop",
                                       task_id=session.active_task)),
            ("processes", DesktopRequest("process_list",
                                         agent="forge-desktop",
                                         task_id=session.active_task)),
            ("system", DesktopRequest("system_info", agent="forge-desktop",
                                      task_id=session.active_task)),
        )
        for name, probe in probes:
            result = self.desktop.observe(probe, profile=profile)
            observations[name] = result.to_dict()
        bridge_payload = self.desktop_bridge.snapshot(
            self._desktop_session(session).bridge_id)
        return {"session_profile": session.profile,
                "profile": profile.mode,
                "observations": redact(observations),
                "provider": redact(bridge_payload)}

    def desktop_act(self, session: Session, action: str, *,
                    target: str = "", params: dict[str, Any] | None = None,
                    reason: str = "", task_id: str = "",
                    approval_id: str = "") -> dict[str, Any]:
        """Execute a desktop action through the complete A35 pipeline."""
        from forge.desktop.actions import DesktopActionKind, DesktopRequest

        try:
            kind = DesktopActionKind(action)
        except ValueError:
            raise InvalidRequest(
                f"Unknown desktop action: {action!r}") from None
        if not isinstance(target, str) or len(target) > 500:
            raise InvalidRequest("Invalid desktop target.")
        if len(reason) > 500:
            raise InvalidRequest("Invalid reason.")
        request = DesktopRequest(
            kind, target=target, params=dict(params or {}),
            agent="forge-desktop", task_id=task_id or session.active_task,
            session_id=session.id,
            reason=reason or f"cockpit desktop {kind.value}")
        result = self.desktop.act(
            request, approval_token_id=approval_id,
            profile=self._desktop_profile(session))
        payload = result.to_dict()
        payload.update({"session_profile": session.profile})
        self._audit(
            session.actor, "desktop", request.policy_operation(),
            result.allowed, task_id=request.task_id,
            reason=(request.reason if result.executed
                    else f"refused: {result.decision}"))
        self._emit(session.active_task or task_id or "desktop",
                   session.project_id, "desktop.action",
                   {"action": kind.value, "target": target,
                    "decision": result.decision,
                    "executed": result.executed,
                    "risk": result.risk,
                    "approval_required": result.approval_required})
        return payload

    def desktop_grant(self, session: Session, task_id: str,
                      scopes: tuple[str, ...] | list[str]) -> dict[str, Any]:
        """Grant a bounded desktop scope to a task.

        A grant only defines what may be *attempted*; every action still
        passes policy, risk invariants, profile, and approval gates.
        """
        from forge.desktop.agent import GrantScopeChecker
        from forge.desktop.actions import DesktopActionKind

        if not isinstance(task_id, str) or not task_id.strip() \
                or len(task_id) > 128:
            raise InvalidRequest("task_id is required.")
        scope_list = [str(item) for item in scopes]
        if not scope_list or len(scope_list) > 32:
            raise InvalidRequest("scopes must be a non-empty list (<=32).")
        allowed_tokens = {"*", "screen", "windows", "input", "clipboard",
                          "files", "processes", "system",
                          "app:*", "window:*", "process:*"}
        for scope in scope_list:
            if not isinstance(scope, str) or len(scope) > 300:
                raise InvalidRequest("Invalid scope entry.")
            if not any(scope == token or scope.startswith(
                    ("app:", "window:", "file:", "process:"))
                    for token in allowed_tokens) \
                    and scope not in allowed_tokens:
                raise InvalidRequest(f"Unknown desktop scope: {scope!r}")
        checker = self.desktop.scope_checker
        if not isinstance(checker, GrantScopeChecker):
            raise InvalidRequest(
                "Desktop task grants need a GrantScopeChecker")
        checker.grant(task_id, tuple(scope_list),
                      ttl=self.config.desktop_bridge_ttl)
        self._audit(session.actor, "desktop", "grant_task", True,
                    task_id=task_id,
                    reason=f"scopes={sorted(scope_list)!r}")
        self._emit(task_id, session.project_id, "desktop.grant",
                   {"task_id": task_id, "scopes": sorted(scope_list)})
        return {"task_id": task_id, "scopes": sorted(scope_list),
                "ttl_seconds": self.config.desktop_bridge_ttl,
                "note": "The grant only scopes attempts; policy, risk "
                        "invariants, profile, and approvals still gate "
                        "every action."}

    def list_desktop_approvals(self, session: Session) -> list[dict[str, Any]]:
        """Desktop approval requests visible to this cockpit session.

        Desktop requests are bound to the session (or its active task),
        so visibility is exact — cross-session desktop approvals are
        simply not listed (never 403).
        """
        from forge.security.approvals import ApprovalStatus

        visible: list[dict[str, Any]] = []
        for request in self.approval_store.all_requests():
            if request.resource != Resource.DESKTOP:
                continue
            if request.status != ApprovalStatus.PENDING:
                continue
            if request.task_id not in (session.id, session.active_task):
                continue
            visible.append(request.to_dict())
        return visible

    def decide_desktop_request(self, session: Session, approval_id: str,
                               approved: bool) -> dict[str, Any]:
        """Decide a desktop approval through the same A33 store.

        This is a cockpit surface for desktop requests (which are
        session-bound, not run-bound); authority remains entirely in the
        A33 :class:`ApprovalStore` — distinct approver, single
        transition, scoped single-use tokens.
        """
        from forge.security.approvals import ApprovalStatus

        validate_id(approval_id, kind="approval id")
        request = self.approval_store.get_request(approval_id)
        if request is None or request.resource != Resource.DESKTOP:
            raise ApprovalNotFoundError(
                f"Unknown approval: {approval_id!r}")
        if request.task_id not in (session.id, session.active_task):
            raise ApprovalNotFoundError(
                f"Unknown approval: {approval_id!r}")
        try:
            decided = self.approval_store.decide(
                approval_id, approved, session.actor)
        except ValueError as exc:
            raise ApprovalConflictError(str(exc)) from exc
        token_id = ""
        if approved:
            token = self.approval_store.issue(
                approval_id, decided_by=session.actor,
                ttl_seconds=self.config.approval_token_ttl, max_uses=1)
            token_id = token.id
        self._audit(session.actor, "desktop",
                    "approve" if approved else "deny", True,
                    task_id=request.task_id,
                    reason=f"desktop approval {approval_id} "
                    f"{'approved' if approved else 'denied'}")
        self._emit(session.active_task or "desktop",
                   session.project_id,
                   "desktop.approved" if approved else "desktop.denied",
                   {"approval_id": approval_id,
                    "operation": f"{request.resource.value}:"
                    f"{request.operation}",
                    "decided_by": session.actor})
        return {"approval": decided.to_dict(), "token_id": token_id}

    # -- health -------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "auth_mode": ("local-dev" if self.config.local_dev_mode
                          else "production"),
            "worker": self.running,
            "projects": len(self.projects),
            "time": time.time(),
        }

    # -- internals ------------------------------------------------------------------

    def _queue_for(self, project_id: str) -> PersistentTaskQueue:
        try:
            return self._queues[project_id]
        except KeyError:
            project = self.get_project(project_id)
            queue = PersistentTaskQueue(
                store=TaskStore(Path(project.root) / ".forge" / "tasks.db"))
            queue.load()
            self._queues[project_id] = queue
            self._locks.setdefault(project_id, threading.Lock())
            self._active.setdefault(project_id, 0)
            return queue

    def _control_for(self, run_id: str) -> SupervisorControl | None:
        with self._controls_lock:
            return self._controls.get(run_id)

    def _emit(self, task_id: str, project_id: str, event_type: str,
              data: dict[str, Any] | None = None) -> None:
        try:
            self.events.append(task_id, project_id, event_type, data or {})
        except Exception:
            pass

    def _audit(self, actor: str, resource: str, operation: str,
               allowed: bool, *, task_id: str = "",
               reason: str = "") -> None:
        try:
            self.audit.record_decision(
                agent=actor, resource=resource, operation=operation,
                scope=task_id or resource, decision=(
                    PolicyDecision.ALLOW if allowed
                    else PolicyDecision.DENY),
                reason=reason, task_id=task_id)
        except Exception:
            pass

    def _dispatch_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self._dispatch_once()
            except Exception:
                pass
            self._dispatch_event.wait(timeout=0.25)
            self._dispatch_event.clear()

    def _dispatch_once(self) -> None:
        if self._executor is None:
            return
        for project_id in list(self.projects):
            if self._stopping.is_set():
                return
            with self._active_lock:
                if (self._active.get(project_id, 0)
                        >= max(1, self.config.max_runs_per_project)):
                    continue
            queue = self._queue_for(project_id)
            try:
                task = queue.start_next()
            except Exception:
                continue
            if task is None:
                continue
            run = self.runs.get(task.id)
            if run is None or run.status != RunStatus.QUEUED:
                # Not ours to start (paused/cancelled/unknown): hand the
                # queue slot back so a resume can pick it up later.
                try:
                    queue.engine.set_status(task.id, TaskStatus.PENDING)
                    queue.store.save(queue.engine._find(task.id))
                except Exception:
                    pass
                continue
            with self._active_lock:
                self._active[project_id] = (
                    self._active.get(project_id, 0) + 1)
            try:
                self._executor.submit(self._execute_run, run.id)
            except Exception:
                with self._active_lock:
                    self._active[project_id] = max(
                        0, self._active.get(project_id, 1) - 1)
                self.runs.mutate(
                    run.id, status=RunStatus.FAILED,
                    error="Worker pool unavailable.",
                    finished_at=time.time())

    def _execute_run(self, run_id: str) -> None:
        run = self.runs.get(run_id)
        if run is None:
            self._release(run_id, "")
            return
        project_id = run.project_id
        try:
            if run.status != RunStatus.QUEUED:
                return
            started = self.runs.compare_and_set(
                run.id, run.version, status=RunStatus.RUNNING,
                stage="starting", started_at=time.time())
            if started is None:
                return
            run = started
            project = self.get_project(project_id)
            self._emit(run.id, project_id, "task.started",
                       {"mode": run.mode})
            control = SupervisorControl()
            with self._controls_lock:
                self._controls[run.id] = control
            control.on_pause_state(
                lambda paused: self._on_pause_state(run.id, paused))
            # Pre-run checkpoint: browser rollback restores from this.
            try:
                from forge.tools.checkpoint import CheckpointManager
                checkpoint = CheckpointManager(project.root).create(
                    f"cockpit-{run.id}")
                self.runs.mutate(run.id, checkpoint_id=checkpoint.id)
                self._register_checkpoint(run, checkpoint, "pre-run")
                self._emit(run.id, project_id, "checkpoint.created",
                           {"checkpoint_id": checkpoint.id})
            except Exception as exc:
                self._finish_run(
                    run.id, RunStatus.FAILED,
                    error=f"Pre-run checkpoint failed: {exc}")
                return
            from forge.core.supervisor import Supervisor
            try:
                mode = OperationMode(run.mode)
            except ValueError:
                mode = OperationMode.ASSISTED
            outcome = Supervisor(
                project.id, project.root).run(
                    run.requirement, approved=False, fabric=self.fabric,
                    mode=mode, policy=self.policy,
                    approval_store=self.approval_store,
                    audit_log=self.audit,
                    approval_callback=self._approval_callback(run.id),
                    on_event=self._supervisor_sink(run.id),
                    control=control)
            self._record_outcome(run.id, outcome)
        except Exception as exc:  # pragma: no cover - defensive
            try:
                self._finish_run(
                    run_id, RunStatus.FAILED,
                    error=f"Worker error: {exc}")
            except Exception:
                pass
        finally:
            with self._controls_lock:
                self._controls.pop(run_id, None)
            self._release(run_id, project_id)

    def _release(self, run_id: str, project_id: str) -> None:
        del run_id
        if not project_id:
            return
        with self._active_lock:
            self._active[project_id] = max(
                0, self._active.get(project_id, 1) - 1)
        self._dispatch_event.set()

    def _on_pause_state(self, run_id: str, paused: bool) -> None:
        run = self.runs.get(run_id)
        if run is None or run.status in TERMINAL_STATUSES:
            return
        if paused and run.status in (RunStatus.RUNNING,
                                     RunStatus.WAITING_APPROVAL):
            self.runs.mutate(run.id, status=RunStatus.PAUSED)
            self._emit(run.id, run.project_id, "task.paused",
                       {"detail": "Paused at a stage boundary."})
        elif not paused and run.status == RunStatus.PAUSED:
            self.runs.mutate(run.id, status=RunStatus.RUNNING,
                             pause_requested=False)
            self._emit(run.id, run.project_id, "task.resumed", {})

    def _supervisor_sink(self, run_id: str) -> Callable[[str, dict], None]:
        def sink(name: str, details: dict[str, Any]) -> None:
            try:
                run = self.runs.get(run_id)
                if run is None:
                    return
                if name == "stage_started":
                    raw = str(details.get("stage", ""))
                    display = DISPLAY_STAGES.get(raw, raw.lower())
                    self.runs.mutate(run.id, stage=display)
                    self._emit(run.id, run.project_id, "stage.started",
                               {"stage": display, "raw": raw})
                    return
                mapped = SUPERVISOR_EVENTS.get(name)
                if mapped is None:
                    return
                if name == "model_selected":
                    self.runs.mutate(
                        run.id, model=str(details.get("model", "")),
                        provider=str(details.get("provider", "")))
                if name == "agents_selected":
                    self._run_extra.setdefault(run_id, {})[
                        "agents"] = details.get("agents", [])
                self._emit(run.id, run.project_id, mapped, details)
            except Exception:
                pass
        return sink

    def _approval_callback(self, run_id: str) -> Callable[[Any], str]:
        def callback(query: Any) -> str:
            from forge.core.run_control import TaskCancelled

            run = self.runs.get(run_id)
            if run is None or run.status in TERMINAL_STATUSES:
                return ""
            control = self._control_for(run_id)
            if control is not None and control.cancel_requested:
                raise TaskCancelled("cancelled before approval")
            # Record the supervisor's task id so approvals map to this run.
            if query.task_id and not run.supervisor_task_id:
                self.runs.mutate(run.id,
                                 supervisor_task_id=query.task_id)
            previous = run.status
            if previous == RunStatus.RUNNING:
                self.runs.mutate(run.id,
                                 status=RunStatus.WAITING_APPROVAL)
            try:
                filed = self.approvals.file_query(
                    query, self.runs.get(run_id) or run,
                    model=run.model, provider=run.provider)
            except ApprovalError:
                if previous == RunStatus.RUNNING:
                    self.runs.mutate(run_id,
                                     status=RunStatus.RUNNING)
                return ""
            try:
                return self.approvals.wait_for_token(
                    filed, control=control,
                    fingerprint=query.fingerprint or "")
            finally:
                current = self.runs.get(run_id)
                if (current is not None
                        and current.status == RunStatus.WAITING_APPROVAL):
                    self.runs.mutate(run_id, status=RunStatus.RUNNING)
        return callback

    def _record_outcome(self, run_id: str, outcome: dict[str, Any]) -> None:
        run = self.runs.get(run_id)
        if run is None:
            return
        report = outcome.get("report", {}) if isinstance(outcome, dict) else {}
        files = [str(path) for path in outcome.get("files", [])
                 if isinstance(path, str)][:500]
        updates: dict[str, Any] = {
            "report_json": json.dumps(redact(report), default=str),
            "files_json": json.dumps(files),
            "model": str(outcome.get("model", "") or run.model),
            "provider": str(outcome.get("provider", "")
                            or run.provider),
            "supervisor_task_id": str(
                (outcome.get("task") or {}).get("id", "")
                or run.supervisor_task_id),
            "finished_at": time.time(),
            "rollback": bool(outcome.get("rollback", False)),
        }
        if outcome.get("accepted"):
            updates.update(status=RunStatus.SUCCEEDED, stage="completed",
                           error="")
        elif outcome.get("cancelled"):
            updates.update(status=RunStatus.CANCELLED, stage="cancelled",
                           error="Cancelled by operator.")
        else:
            updates.update(
                status=RunStatus.FAILED, stage="failed",
                error=str(outcome.get("error", "Run failed."))[:2000])
        self.runs.mutate(run_id, **updates)
        finished = self.runs.get(run_id)
        if finished is None:
            return
        if finished.status == RunStatus.SUCCEEDED:
            self._mirror_complete(finished)
            self._emit(run_id, finished.project_id, "task.completed",
                       {"files": files, "model": finished.model,
                        "provider": finished.provider})
        elif finished.status == RunStatus.CANCELLED:
            self._mirror_fail(finished, "Cancelled by operator.")
            self._emit(run_id, finished.project_id, "task.cancelled",
                       {"rollback": finished.rollback})
        else:
            self._mirror_fail(finished, finished.error)
            self._emit(run_id, finished.project_id, "task.failed",
                       {"error": finished.error,
                        "rollback": finished.rollback})
        self._audit(finished.actor or "worker", "task", "finish",
                    finished.status == RunStatus.SUCCEEDED,
                    task_id=run_id,
                    reason=f"status {finished.status.value}")

    def _finish_run(self, run_id: str, status: RunStatus,
                    *, error: str = "") -> None:
        run = self.runs.get(run_id)
        if run is None or run.status in TERMINAL_STATUSES:
            return
        self.runs.mutate(run.id, status=status,
                         stage=status.value.lower(),
                         error=error[:2000], finished_at=time.time())
        if status == RunStatus.SUCCEEDED:
            self._mirror_complete(run)
        else:
            self._mirror_fail(run, error or status.value)
        self._emit(run.id, run.project_id,
                   "task.completed" if status == RunStatus.SUCCEEDED
                   else "task.failed",
                   {"error": error} if error else {})

    def _mirror_complete(self, run: Run) -> None:
        try:
            self._queue_for(run.project_id).complete(run.id)
        except Exception:
            pass

    def _mirror_fail(self, run: Run, error: str) -> None:
        try:
            self._queue_for(run.project_id).fail(run.id, error[:500])
        except Exception:
            pass

    def close(self) -> None:
        self.stop(wait=True)
        try:
            self._db.close()
        except Exception:
            pass

