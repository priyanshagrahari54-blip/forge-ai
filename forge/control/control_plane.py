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
from forge.control.memory import VALID_KINDS as VALID_MEMORY_KINDS
from forge.control.memory import SessionMemoryStore
from forge.control.orchestrations import (OrchestrationStatus,
                                          OrchestrationStore)
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


class VoiceUnavailable(ControlError):
    """The voice stack could not serve a request (structured, honest)."""

    code = "VOICE_UNAVAILABLE"
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
    # A36 voice: provider names for speech-to-text / text-to-speech.
    # Only the deterministic simulated providers exist in A36; real
    # providers register behind the same protocols as plugins.
    voice_stt_provider: str = "simulated"
    voice_tts_provider: str = "simulated"
    # A37 memory: record bounded run-outcome summaries into durable
    # project memory when runs finish (system observability, no secrets).
    memory_record_runs: bool = True
    # A38 multi-agent orchestration: team execution budgets. Independent
    # steps run in parallel (bounded by workers); steps are capped by
    # attempts and an optional per-step timeout, and any dispatch is
    # gated by the A33 policy (Resource.AGENT / execute).
    orchestration_max_workers: int = 3
    orchestration_max_attempts: int = 2
    orchestration_step_timeout: float | None = 120.0
    # A39 vision: provider behind the Vision interface. Only the
    # deterministic simulated provider exists in A39 (honestly labeled,
    # no OCR/model); real providers register as plugins.
    vision_provider: str = "simulated"
    # A40 computer use: hard cap on executed actions per task.
    computer_max_actions: int = 20
    # A48 compute: local execution budgets per session.
    compute_max_cells: int = 20
    compute_max_seconds: float = 300.0
    compute_cell_timeout: float = 30.0

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
        for key, env_name in (("voice_stt_provider", "FORGE_VOICE_STT_PROVIDER"),
                              ("voice_tts_provider", "FORGE_VOICE_TTS_PROVIDER")):
            name = os.environ.get(env_name, "").strip()
            if name:
                if name != "simulated":
                    raise ValueError(
                        f"Unknown {env_name} {name!r}; A36 supports "
                        "'simulated' only")
                setattr(config, key, name)
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
        self.orchestrations = OrchestrationStore(self._db)
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
        # A36 voice: the simulated speech stack (STT/TTS/wake) behind the
        # A33 voice permission layer. Real providers register as plugins.
        self._voice_session: Any = None
        # A37 memory: session-scoped durable memory + per-project durable
        # knowledge stores (lazily built over each project root).
        self.session_memory = SessionMemoryStore(self._db)
        self._memory_stores: dict[str, Any] = {}
        # A39 vision: provider-independent image understanding.
        from forge.vision.pipeline import build_vision_provider
        self.vision_provider = build_vision_provider(config.vision_provider)
        # A40 computer use: the vision-driven, policy-gated control loop
        # over the A35 desktop agent (screen -> understand -> element
        # tree -> propose -> gate -> execute).
        from forge.computer.engine import ComputerUseEngine
        self.computer = ComputerUseEngine(
            self.desktop,
            max_actions_per_task=config.computer_max_actions)
        self._queues: dict[str, PersistentTaskQueue] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._active: dict[str, int] = {}
        self._active_lock = threading.Lock()
        self._controls: dict[str, SupervisorControl] = {}
        self._controls_lock = threading.Lock()
        self._orch_lock = threading.Lock()
        self._orch_controls: dict[str, SupervisorControl] = {}
        self._orch_events: dict[str, threading.Event] = {}
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
        try:
            self._metrics().incr("tasks.submitted")
        except Exception:
            pass
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

    # -- voice loop (A36) ----------------------------------------------------------

    #: Voice intents that map onto real task creation (A36 execution).
    VOICE_INTENT_REQUIREMENTS = {
        "run_tests": "Run the full test suite and fix any failures.",
        "commit": "Review and commit the current changes with a clear "
                  "message.",
        "update_website": "Update the project website per the latest "
                          "changes.",
        "summarize": "Summarize {target}.",
        "review": "Review {target} and report findings.",
    }

    def _voice_stack(self) -> Any:
        """The simulated voice loop (A36 providers only, honestly labeled)."""
        from forge.voice import (SimulatedSpeechSynthesizer,
                                 SimulatedSpeechToText,
                                 SimulatedWakeWordDetector, VoiceInterface,
                                 VoiceSession)

        if self._voice_session is None:
            if self.config.voice_stt_provider != "simulated" or \
                    self.config.voice_tts_provider != "simulated":
                raise ValueError(
                    "Only the simulated voice providers exist in A36")
            self._voice_session = VoiceSession(
                VoiceInterface(policy=self.policy,
                               store=self.approval_store,
                               audit=self.audit),
                transcriber=SimulatedSpeechToText(),
                synthesizer=SimulatedSpeechSynthesizer(),
                wake=SimulatedWakeWordDetector())
        return self._voice_session

    def voice_capabilities(self, session: Session) -> dict[str, Any]:
        """Honest voice capability report for the cockpit."""
        del session  # provider matrix is not session-specific
        stack = self._voice_stack()
        return {
            "status": "simulation",
            "note": "A36 runs the voice loop through deterministic "
                    "simulated speech (tone codec). Real speech "
                    "recognition/synthesis plug in as providers; the "
                    "simulated recognizer refuses real audio.",
            **stack.capabilities(),
        }

    def voice_synthesize(self, session: Session,
                         text: str) -> dict[str, Any]:
        """Synthesize bounded text into WAV (base64) for playback."""
        from forge.voice.synthesizer import SynthesisError

        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise InvalidRequest("Voice text must be 1-2000 characters.")
        stack = self._voice_stack()
        try:
            chunk = stack.speak(text.strip())
        except SynthesisError as exc:
            raise VoiceUnavailable(f"{exc.kind}: {exc.message}") from exc
        payload = self._voice_audio_payload(chunk,
                                            engine=stack.synthesizer.name,
                                            simulation=bool(getattr(
                                                stack.synthesizer,
                                                "simulation", False)))
        self._audit(session.actor, "voice", "synthesize", True,
                    reason=f"spoke {len(text)} chars")
        return payload

    def voice_transcribe(self, session: Session,
                         audio_b64: str) -> dict[str, Any]:
        """Transcribe bounded WAV audio (simulation codec only)."""
        from forge.voice import AudioError, TranscriptionError

        try:
            chunk = self._voice_audio_chunk(audio_b64)
        except AudioError as exc:
            raise InvalidRequest(f"Invalid audio: {exc.message}") from exc
        stack = self._voice_stack()
        try:
            transcription = stack.transcriber.transcribe(chunk)
        except TranscriptionError as exc:
            raise VoiceUnavailable(f"{exc.kind}: {exc.message}") from exc
        self._audit(session.actor, "voice", "transcribe", True,
                    reason=f"recognized {len(transcription.text)} chars")
        return {"transcription": transcription.to_dict()}

    def voice_process(self, session: Session, *,
                      text: str = "", audio_b64: str = "",
                      approval_id: str = "", task_id: str = "",
                      require_wake: bool = True) -> dict[str, Any]:
        """One voice loop: wake → transcribe → A33 permission → act → reply.

        Voice execution is real in A36 — but it runs through the exact
        same policy/approval path as every other surface; a voice command
        can never bypass permissions.
        """
        from forge.voice import AudioError

        if text and audio_b64:
            raise InvalidRequest("Provide either text or audio, not both.")
        speech: Any
        input_mode: str
        if text:
            if not isinstance(text, str) or not text.strip() \
                    or len(text) > 2000:
                raise InvalidRequest("Voice text must be 1-2000 characters.")
            speech = text.strip()
            input_mode = "text"
        elif audio_b64:
            try:
                speech = self._voice_audio_chunk(audio_b64)
            except AudioError as exc:
                raise InvalidRequest(f"Invalid audio: {exc.message}") from exc
            input_mode = "audio"
        else:
            raise InvalidRequest("Provide text or audio for the voice loop.")
        if len(approval_id) > 128 or len(task_id) > 128:
            raise InvalidRequest("Invalid voice request fields.")
        # Approval/audit binding follows the desktop pattern: session id
        # when no task is active, so a human can decide it in the cockpit.
        command_task = task_id or session.active_task or session.id
        stack = self._voice_stack()
        # Distinct agent identity so a human session actor can approve the
        # voice request (the A33 store enforces approver != agent).
        result = stack.process(
            speech, agent="forge-voice", task_id=command_task,
            task_factory=self._voice_task_factory(session),
            approval_token_id=approval_id,
            require_wake=require_wake)
        payload = result.to_dict()
        payload["input_mode"] = input_mode
        if result.response_audio is not None:
            payload["response_audio"].update(self._voice_audio_payload(
                result.response_audio, engine=result.response_engine,
                simulation=result.response_simulation))
        self._audit(session.actor, "voice", "process", result.ok,
                    task_id=command_task,
                    reason=(payload.get("intent") or {}).get("name",
                                                             "voice loop"))
        self._emit(command_task, session.project_id,
                   "voice.processed" if result.ok else "voice.rejected",
                   {"input_mode": input_mode,
                    "intent": (payload.get("intent") or {}).get("name", ""),
                    "executed": bool(result.ok and result.action)})
        return redact(payload)

    def list_voice_approvals(self, session: Session) -> list[dict[str, Any]]:
        """Voice approval requests visible to this cockpit session."""
        from forge.security.approvals import ApprovalStatus

        visible: list[dict[str, Any]] = []
        for request in self.approval_store.all_requests():
            if request.resource != Resource.VOICE:
                continue
            if request.task_id not in (session.id, session.active_task):
                continue
            if request.status != ApprovalStatus.PENDING:
                continue
            visible.append({
                "id": request.id,
                "operation": request.operation,
                "scopes": list(request.scopes),
                "task_id": request.task_id,
                "reason": request.reason,
                "agent": request.agent,
                "created_at": request.created_at,
            })
        return visible

    def decide_voice_request(self, session: Session, approval_id: str,
                             approved: bool) -> dict[str, Any]:
        """Decide a voice approval through the same A33 store (single-use
        tokens on approve)."""
        from forge.security.approvals import ApprovalStatus

        validate_id(approval_id, kind="approval id")
        request = self.approval_store.get_request(approval_id)
        if request is None or request.resource != Resource.VOICE:
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
            uses = len(request.files) if request.files \
                else len(request.scopes)
            token = self.approval_store.issue(
                approval_id, decided_by=session.actor,
                ttl_seconds=self.config.approval_token_ttl,
                max_uses=max(1, uses))
            token_id = token.id
        self._audit(session.actor, "voice",
                    "approve" if approved else "deny", True,
                    task_id=request.task_id,
                    reason=f"voice approval {approval_id} "
                    f"{'approved' if approved else 'denied'}")
        self._emit(session.active_task or "voice", session.project_id,
                   "voice.approved" if approved else "voice.denied",
                   {"approval_id": approval_id,
                    "operation": f"{request.resource.value}:"
                    f"{request.operation}",
                    "decided_by": session.actor})
        return {"approval": decided.to_dict(), "token_id": token_id}

    # -- voice internals ----------------------------------------------------------

    def _voice_task_factory(self, session: Session):
        """Map a parsed voice intent onto a real (policy-gated) action."""
        def factory(intent: "VoiceIntent") -> dict[str, Any]:
            name = intent.name
            slots = dict(intent.slots)
            if name == "status":
                try:
                    counts = self.dashboard(session)["tasks"]
                    text = (f"You have {counts['running']} running, "
                            f"{counts['waiting_approval']} waiting for "
                            f"approval, and {counts['failed']} failed "
                            "tasks.")
                except Exception:
                    text = "Status is unavailable right now."
                return {"kind": "reply", "text": text}
            requirement = self.VOICE_INTENT_REQUIREMENTS.get(name)
            if requirement is None:
                return {"kind": "unhandled", "intent": name}
            try:
                requirement = requirement.format(
                    **{key: value or "the project"
                       for key, value in slots.items()})
                run = self.submit_task(session, requirement)
            except ControlError as exc:
                return {"kind": "error", "code": exc.code,
                        "message": str(exc)}
            return {"kind": "task", **run.to_dict()}
        return factory

    @staticmethod
    def _voice_audio_chunk(audio_b64: str) -> Any:
        import base64

        from forge.voice import AudioError, read_wav

        if not isinstance(audio_b64, str) or not audio_b64:
            raise AudioError("empty", "no audio provided")
        try:
            raw = base64.b64decode(audio_b64, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise AudioError("malformed", "audio is not valid base64") \
                from exc
        return read_wav(raw)

    @staticmethod
    def _voice_audio_payload(chunk: Any, *, engine: str,
                             simulation: bool) -> dict[str, Any]:
        import base64

        wav = chunk.wav()
        return {
            "audio_b64": base64.b64encode(wav).decode("ascii"),
            "mime": "audio/wav",
            "engine": engine,
            "simulation": simulation,
            "duration_ms": chunk.duration_ms,
        }

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


    # -- persistent memory (A37) --------------------------------------------------

    def _project_memory(self, project_id: str):
        """The durable project knowledge store (bounded, path-safe)."""
        from forge.memory.store import MemoryStore

        store = self._memory_stores.get(project_id)
        if store is None:
            project = self.get_project(project_id)
            store = MemoryStore(
                root=str(Path(project.root) / ".forge" / "memory"))
            self._memory_stores[project_id] = store
        return store

    def _memory_permission(self, session: Session, operation: str,
                           scope: str, *, approval_id: str = "",
                           file_approval: bool = True) -> dict[str, Any]:
        """Evaluate one Resource.MEMORY access through the A33 gate."""
        from forge.security.approvals import (ApprovalRequest,
                                              enforce_with_token)
        from forge.security.policy import PermissionRequest

        policy = self.policy if self.policy is not None else PermissionPolicy()
        permission = PermissionRequest(
            agent="forge-memory", resource=Resource.MEMORY,
            operation=operation, scope=scope,
            task_id=session.active_task or session.id,
            reason=f"memory {operation} {scope}")
        evaluation = policy.evaluate(permission)
        self.audit.record_evaluation(permission, evaluation)
        if evaluation.decision == PolicyDecision.REQUIRE_APPROVAL \
                and approval_id:
            allowed, _reason = enforce_with_token(
                self.approval_store, approval_id, permission)
            if allowed:
                return {"allowed": True, "decision": "ALLOW",
                        "approval_required": False,
                        "approval_request_id": approval_id}
        if evaluation.decision == PolicyDecision.REQUIRE_APPROVAL:
            request_id = ""
            if file_approval:
                filed = self.approval_store.submit(ApprovalRequest(
                    agent="forge-memory", resource=Resource.MEMORY,
                    operation=operation, scopes=(scope,),
                    task_id=session.active_task or session.id,
                    reason=f"Memory {operation} on {scope!r}",
                    consequences="The session will change remembered "
                                 "state."))
                request_id = filed.id
            return {"allowed": False, "decision": "REQUIRE_APPROVAL",
                    "approval_required": True,
                    "approval_request_id": request_id,
                    "reason": evaluation.reason}
        if evaluation.decision == PolicyDecision.ALLOW:
            return {"allowed": True, "decision": "ALLOW",
                    "approval_required": False, "approval_request_id": ""}
        return {"allowed": False, "decision": "DENY",
                "approval_required": False, "approval_request_id": "",
                "reason": evaluation.reason}

    def memory_overview(self, session: Session) -> dict[str, Any]:
        """Session memory entries + project keys this session may read."""
        session_entries = [
            entry.to_dict(include_content=False)
            for entry in self.session_memory.list(session.id)
            if self._memory_permission(
                session, "read", f"session:{entry.kind}",
                file_approval=False)["allowed"]]
        project_keys = [
            key for key in self._project_memory(session.project_id).list()
            if self._memory_permission(
                session, "read", key, file_approval=False)["allowed"]]
        return {"session_entries": session_entries,
                "project_keys": project_keys}

    def memory_add(self, session: Session, kind: str, content: str, *,
                   approval_id: str = "") -> dict[str, Any]:
        if kind not in VALID_MEMORY_KINDS:
            raise InvalidRequest(
                f"Memory kind must be one of {VALID_MEMORY_KINDS}: {kind!r}")
        if not isinstance(content, str) or not content.strip() \
                or len(content.encode("utf-8")) > 20_000:
            raise InvalidRequest("Memory content must be 1-20000 bytes.")
        permission = self._memory_permission(
            session, "write", f"session:{kind}", approval_id=approval_id)
        if not permission["allowed"]:
            if permission["approval_required"]:
                return permission
            raise PolicyDenied(
                permission.get("reason") or "Memory write denied by policy.")
        try:
            entry = self.session_memory.add(
                session.id, kind, content, source=session.actor)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "memory", "write", True,
                    task_id=session.id, reason=f"session:{kind}")
        return {"allowed": True, "entry": entry.to_dict()}

    def memory_get(self, session: Session, entry_id: str, *,
                   approval_id: str = "") -> dict[str, Any]:
        validate_id(entry_id, kind="memory entry id")
        entry = self.session_memory.get(session.id, entry_id)
        if entry is None:
            raise NotFound("Unknown memory entry.")
        permission = self._memory_permission(
            session, "read", f"session:{entry.kind}", approval_id=approval_id)
        if not permission["allowed"]:
            if permission["approval_required"]:
                return {"allowed": False, **permission}
            raise PolicyDenied(
                permission.get("reason") or "Memory read denied by policy.")
        return {"allowed": True, "entry": redact(entry.to_dict())}

    def memory_delete(self, session: Session, entry_id: str, *,
                      approval_id: str = "") -> dict[str, Any]:
        validate_id(entry_id, kind="memory entry id")
        entry = self.session_memory.get(session.id, entry_id)
        if entry is None:
            raise NotFound("Unknown memory entry.")
        permission = self._memory_permission(
            session, "delete", f"session:{entry.kind}",
            approval_id=approval_id)
        if not permission["allowed"]:
            if permission["approval_required"]:
                return permission
            raise PolicyDenied(
                permission.get("reason") or "Memory delete denied by policy.")
        self.session_memory.delete(session.id, entry_id)
        self._audit(session.actor, "memory", "delete", True,
                    task_id=session.id, reason=f"session:{entry.kind}")
        return {"allowed": True, "deleted": entry_id}

    def memory_project_save(self, session: Session, key: str,
                            content: str, *,
                            approval_id: str = "") -> dict[str, Any]:
        if not isinstance(key, str) or not key.strip() or len(key) > 256:
            raise InvalidRequest("Memory key must be 1-256 characters.")
        if not isinstance(content, str) or not content.strip() \
                or len(content.encode("utf-8")) > 20_000:
            raise InvalidRequest("Memory content must be 1-20000 bytes.")
        permission = self._memory_permission(
            session, "write", key, approval_id=approval_id)
        if not permission["allowed"]:
            if permission["approval_required"]:
                return permission
            raise PolicyDenied(
                permission.get("reason") or "Memory write denied by policy.")
        try:
            self._project_memory(session.project_id).save(key, content)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "memory", "write", True,
                    task_id=session.id, reason=f"project:{key}")
        return {"allowed": True, "key": key}

    def memory_project_load(self, session: Session,
                            key: str) -> dict[str, Any]:
        if not isinstance(key, str) or not key.strip() or len(key) > 256:
            raise InvalidRequest("Memory key must be 1-256 characters.")
        permission = self._memory_permission(
            session, "read", key, file_approval=False)
        if not permission["allowed"]:
            raise PolicyDenied(
                permission.get("reason") or "Memory read denied by policy.")
        try:
            content = self._project_memory(session.project_id).load(key)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        return {"key": key, "content": redact(content) if content else None}

    def list_memory_approvals(self, session: Session) -> list[dict[str, Any]]:
        from forge.security.approvals import ApprovalStatus

        visible: list[dict[str, Any]] = []
        for request in self.approval_store.all_requests():
            if request.resource != Resource.MEMORY:
                continue
            if request.task_id not in (session.id, session.active_task):
                continue
            if request.status != ApprovalStatus.PENDING:
                continue
            visible.append({
                "id": request.id, "operation": request.operation,
                "scopes": list(request.scopes), "task_id": request.task_id,
                "reason": request.reason, "agent": request.agent,
                "created_at": request.created_at,
            })
        return visible

    def decide_memory_request(self, session: Session, approval_id: str,
                              approved: bool) -> dict[str, Any]:
        request = self.approval_store.get_request(approval_id)
        if request is None or request.resource != Resource.MEMORY:
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
        self._audit(session.actor, "memory",
                    "approve" if approved else "deny", True,
                    task_id=request.task_id,
                    reason=f"memory approval {approval_id} "
                    f"{'approved' if approved else 'denied'}")
        return {"approval": decided.to_dict(), "token_id": token_id}




    # -- computer use (A40) --------------------------------------------------------

    def computer_observe(self, session: Session, image_b64: str, *,
                         goal: str = "") -> dict[str, Any]:
        """Observe a screen snapshot through the permissioned vision
        provider and record it as a versioned snapshot."""
        if not isinstance(goal, str) or len(goal) > 1000:
            raise InvalidRequest("Invalid goal.")
        analyzed = self.vision_analyze(session, image_b64)
        if not analyzed.get("allowed"):
            raise PolicyDenied(
                analyzed.get("summary", "vision analyze denied"))
        import base64
        image = base64.b64decode(image_b64, validate=True)
        task_id = session.active_task or session.id
        payload = self.computer.observe(
            session.id, task_id, image, goal=goal,
            understanding=analyzed)
        self._audit(session.actor, "computer", "observe", True,
                    task_id=task_id,
                    reason=f"snapshot {payload['snapshot_version']}")
        return payload

    def computer_propose(self, session: Session, image_b64: str, *,
                         goal: str = "") -> dict[str, Any]:
        """Dry-run computer-use proposals from one screen. Nothing runs."""
        analyzed = self.vision_analyze(session, image_b64)
        if not analyzed.get("allowed"):
            raise PolicyDenied(
                analyzed.get("summary", "vision analyze denied"))
        task_id = session.active_task or session.id
        payload = self.computer.propose(
            session.id, task_id, goal=goal, understanding=analyzed,
            profile=self._desktop_profile(session))
        self._audit(session.actor, "computer", "propose", True,
                    task_id=task_id,
                    reason=f"{len(payload['proposals'])} proposals, "
                           "executed=False")
        return payload

    def computer_act(self, session: Session, action: str, target: str = "",
                     params: dict[str, Any] | None = None,
                     reason: str = "", approval_id: str = ""
                     ) -> dict[str, Any]:
        """Authorize and execute ONE computer action, fully guarded.

        SAFE/LOCKED sessions may only observe; HIGH/CRITICAL risk is
        escalated to operator approval; confirmation dialogs on screen
        fail closed; the per-task action budget is enforced.
        """
        from forge.desktop.actions import DesktopActionKind, DesktopRequest

        try:
            kind = DesktopActionKind(action)
        except ValueError:
            raise InvalidRequest(f"Unknown computer action: {action!r}") \
                from None
        if not isinstance(target, str) or len(target) > 500:
            raise InvalidRequest("Invalid computer target.")
        if not isinstance(reason, str) or len(reason) > 1000:
            raise InvalidRequest("Invalid reason.")
        task_id = session.active_task or session.id
        request = DesktopRequest(
            kind, target=target, params=dict(params or {}),
            agent="forge-computer", task_id=task_id,
            session_id=session.id,
            reason=reason or "cockpit computer action")
        payload = self.computer.act(
            session.id, task_id, request,
            mode=OperationMode(session.profile),
            approval_token_id=approval_id,
            profile=self._desktop_profile(session))
        self._audit(session.actor, "computer", action,
                    bool(payload.get("executed")), task_id=task_id,
                    reason=payload.get("reason", "")[:300])
        return payload

    def computer_cycle(self, session: Session, image_b64: str, *,
                       goal: str = "", max_cycles: int = 5) -> dict[str, Any]:
        """One bounded observe -> propose -> act round."""
        analyzed = self.vision_analyze(session, image_b64)
        if not analyzed.get("allowed"):
            raise PolicyDenied(
                analyzed.get("summary", "vision analyze denied"))
        import base64
        image = base64.b64decode(image_b64, validate=True)
        task_id = session.active_task or session.id
        payload = self.computer.cycle(
            session.id, task_id, image, goal=goal, understanding=analyzed,
            mode=OperationMode(session.profile),
            profile=self._desktop_profile(session),
            max_cycles=max_cycles)
        self._audit(session.actor, "computer", "cycle", True,
                    task_id=task_id,
                    reason=payload["note"][:200])
        return payload

    def computer_history(self, session: Session) -> dict[str, Any]:
        """Versioned snapshots and redacted action history for the task."""
        task_id = session.active_task or session.id
        return self.computer.history(task_id)

    def list_computer_approvals(self, session: Session) -> list[dict[str, Any]]:
        visible = []
        for request in self.approval_store.pending():
            if request.agent == "forge-computer" and request.task_id in (
                    session.id, session.active_task):
                visible.append(request.to_dict())
        return visible

    def decide_computer_approval(self, session: Session, approval_id: str,
                                 approved: bool) -> dict[str, Any]:
        request = self.approval_store.get_request(approval_id)
        if request is None or request.agent != "forge-computer" \
                or request.task_id not in (session.id, session.active_task):
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
        self._audit(session.actor, "computer",
                    "approve" if approved else "deny", True,
                    task_id=request.task_id,
                    reason=f"computer approval {approval_id}")
        return {"approval": decided.to_dict(), "token_id": token_id}



    # -- cockpit catalog surfaces (A41) ----------------------------------------------

    def agent_catalog(self) -> list[dict[str, Any]]:
        """The documented agent inventory with its real security gating.

        Catalog rows describe which agents exist in this build, what
        capabilities they serve, and which A33 resource gates their
        actions. This is architecture metadata — never a capability
        claim beyond what the gates and executors actually enforce.
        """
        return [
            {"name": "planner", "role": "planning",
             "capabilities": ["planning"],
             "gate": "read-only; deterministic requirement analysis",
             "real": True,
             "notes": "CapabilityAgentPlanner over the agent registry."},
            {"name": "coder", "role": "coding",
             "capabilities": ["coding"],
             "gate": "FILESYSTEM write via change sets; operator approval "
                     "under ASSISTED",
             "real": True,
             "notes": "Proposes change sets; never writes ungated."},
            {"name": "debugger", "role": "debugging",
             "capabilities": ["debugging"],
             "gate": "FILESYSTEM write via change sets; TERMINAL execute "
                     "for tests",
             "real": True,
             "notes": "Test/Debug loop with bounded retries."},
            {"name": "tester", "role": "testing",
             "capabilities": ["testing"],
             "gate": "TERMINAL execute gated per command",
             "real": True,
             "notes": "Runs the project test suite; reports real counts."},
            {"name": "reviewer", "role": "reviewing",
             "capabilities": ["review"],
             "gate": "read-only",
             "real": True,
             "notes": "Independent review pass over the change set."},
            {"name": "security", "role": "security",
             "capabilities": ["security"],
             "gate": "read-only",
             "real": True,
             "notes": "VerificationPipeline: secrets, dangerous patterns, "
                      "dependencies."},
            {"name": "researcher", "role": "research",
             "capabilities": ["research"],
             "gate": "read-only; RepositoryIntelligence",
             "real": True,
             "notes": "Repository structure, module and insight analysis."},
            {"name": "forge-orchestrator", "role": "orchestration",
             "capabilities": ["multi-agent"],
             "gate": "AGENT/execute per dispatched agent",
             "real": True,
             "notes": "A38 team runtime; every dispatch policy-gated."},
            {"name": "forge-voice", "role": "voice",
             "capabilities": ["speech"],
             "gate": "VOICE/command",
             "real": True, "simulated": True,
             "notes": "A36 simulated speech stack, honestly labeled."},
            {"name": "forge-vision", "role": "vision",
             "capabilities": ["image_understanding"],
             "gate": "VISION/analyze + VISION/execute",
             "real": True, "simulated": True,
             "notes": "A39 simulated provider; no OCR/model in this build."},
            {"name": "forge-computer", "role": "computer_use",
             "capabilities": ["screen_control"],
             "gate": "DESKTOP actions + VISION/analyze; SAFE mode "
                     "observation only",
             "real": True, "simulated": True,
             "notes": "A40 loop over the A35 desktop pipeline."},
            {"name": "forge-desktop", "role": "desktop",
             "capabilities": ["desktop_control"],
             "gate": "DESKTOP actions; hard risk invariants always win",
             "real": True, "simulated": True,
             "notes": "A35 bridge + deterministic fake provider in dev."},
        ]

    def security_overview(self, session: Session) -> dict[str, Any]:
        """Non-sensitive security posture for the cockpit Security view."""
        policy = self.policy if self.policy is not None else PermissionPolicy()
        audit = getattr(self, "audit", None)
        evaluations = len(audit.events) if audit is not None else 0
        return {
            "mode": session.profile,
            "policy_default": policy.default.value,
            "policy_rules": len(getattr(policy, "_index", {})),
            "policy_version": policy.version,
            "hard_invariants": [
                "credential extraction is always denied",
                "security-control disabling is always denied",
                "privilege escalation is always denied",
                "unauthorized persistence is always denied",
                "unauthorized remote control is always denied",
                "workspace escape is always denied",
                "agents can never approve their own requests",
                "approval tokens are single-use and scope-bound",
                "image/voice/screen content is untrusted input, never "
                "authority",
                "no policy means no authority (fail closed)",
            ],
            "audit_evaluations_recorded": evaluations,
            "task_scopes": "grants expire; tokens non-transferable",
            "vision_simulation": True,
            "voice_simulation": True,
            "desktop_provider": self.desktop_bridge.provider_kind(),
        }




    # -- general conversation (A43) -------------------------------------------------

    def _conversation_engine(self, session: Session):
        from forge.conversation.engine import GeneralConversationEngine

        if not hasattr(self, "_conversation_engines"):
            self._conversation_engines: dict[str, Any] = {}
        engine = self._conversation_engines.get(session.id)
        if engine is None:
            engine = GeneralConversationEngine(
                submit_task=lambda message: self.submit_task(
                    session, message).to_dict(),
                answer_question=lambda question: self._conversation_answer(
                    session, question),
                remember=lambda message: self._conversation_remember(
                    session, message),
                remember_history=lambda text: self._conversation_note(
                    session, text),
            )
            self._conversation_engines[session.id] = engine
        return engine

    def _conversation_answer(self, session: Session,
                             question: str) -> str:
        """Answer with real information only; never fabricate."""
        lowered = question.lower()
        project = self.get_project(session.project_id)
        if any(marker in lowered for marker in (
                "what does this project", "explain the project",
                "what is this project", "describe the project",
                "what files", "structure of the project",
                "what does the codebase")):
            try:
                from forge.intelligence.repository import (
                    RepositoryIntelligence)
                intelligence = RepositoryIntelligence.build(project.root)
                summary = intelligence.summary()
                points = ", ".join(summary.get("entry_points", [])
                                   or ["none detected"])[:400]
                return (
                    "This project has "
                    f"{summary.get('source_file_count', 0)} source files, "
                    f"{summary.get('test_file_count', 0)} test files, and "
                    f"{summary.get('package_count', 0)} packages. "
                    f"Entry points: {points}.")
            except Exception as exc:
                return f"I couldn't analyze the repository: {exc}"
        if any(marker in lowered for marker in (
                "what models", "which models", "available models",
                "model fabric", "what providers", "which providers")):
            try:
                state = self.get_model_state()
                names = ", ".join(
                    item.get("name", "?") for item in state["models"][:5]
                ) or "none registered"
                providers = ", ".join(
                    item.get("name", "?") for item in state["providers"][:5]
                ) or "none registered"
                return (f"I have {len(state['models'])} model(s) "
                        f"registered: {names}. Providers: {providers}. "
                        "The built-in local provider is a deterministic "
                        "no-op; real providers need configuration.")
            except Exception:
                return "Model registry status is unavailable right now."
        if any(marker in lowered for marker in (
                "how many tasks", "task status", "status of tasks",
                "what is running")):
            try:
                counts = self.dashboard(session)["tasks"]
                return (f"You have {counts['running']} running, "
                        f"{counts['waiting_approval']} waiting for "
                        f"approval, and {counts['failed']} failed tasks.")
            except Exception:
                return "Task status is unavailable right now."
        if any(marker in lowered for marker in (
                "what do you remember", "my preferences",
                "what did i tell you")):
            entries = [entry.to_dict() for entry in
                       self.session_memory.list(session.id)]
            if not entries:
                return "I don't have anything remembered for you yet."
            notes = [entry.get("content", "") for entry in entries[:5]]
            return "I remember: " + " | ".join(notes)[:800]
        return ("I don't have a real answer for that. I can analyze "
                "this repository, report task status, and recall what "
                "you asked me to remember.")

    def _conversation_remember(self, session: Session,
                               message: str) -> bool:
        """Persist a preference through the normal memory gate."""
        try:
            self.memory_add(session, "fact", f"preference: {message}")
            return True
        except Exception:
            return False

    def _conversation_note(self, session: Session, text: str) -> None:
        """Record a bounded conversation summary through the memory gate."""
        try:
            self.memory_add(session, "summary", f"conversation: {text}")
        except Exception:
            pass

    def converse(self, session: Session, message: str) -> dict[str, Any]:
        """Classify and route one message; answer with real data."""
        engine = self._conversation_engine(session)
        payload = engine.converse(message)
        self._audit(session.actor, "conversation", payload["kind"], True,
                    task_id=session.active_task or session.id,
                    reason=payload["reply"][:300])
        return payload

    def conversation_history(self, session: Session) -> dict[str, Any]:
        engine = self._conversation_engine(session)
        return {"history": engine.snapshot()}








    # -- agent evolution (A50) -----------------------------------------------------------------

    def _agent_evolution(self, session: Session):
        from forge.agents.evolution import AgentEvolution

        if not hasattr(self, "_agent_evolutions"):
            self._agent_evolutions: dict[str, Any] = {}
        evolution = self._agent_evolutions.get(session.id)
        if evolution is None:
            evolution = AgentEvolution(session.id)
            self._agent_evolutions[session.id] = evolution
        return evolution

    def agent_record_outcome(self, session: Session, agent_name: str,
                             task_id: str) -> dict[str, Any]:
        """Incorporate one real terminal run into the agent's ledger."""
        factory = self._agent_factory(session)
        definition = factory.get(agent_name)
        if definition is None:
            raise InvalidRequest(f"Unknown agent: {agent_name}")
        run = self.runs.get(validate_id(task_id, kind="task id"))
        if run is None:
            raise InvalidRequest(f"Unknown task: {task_id}")
        if run.status not in TERMINAL_STATUSES:
            raise InvalidRequest(
                f"Task {task_id} has not finished yet "
                f"(status={run.status.value}); only real terminal "
                "outcomes evolve an agent.")
        evolution = self._agent_evolution(session)
        metrics = evolution.record(definition, run)
        self._audit(session.actor, "agents", "evolve", True,
                    task_id=run.id,
                    reason=f"{agent_name} generation="
                           f"{definition.generation} "
                           f"outcome={metrics['last_outcome']}")
        return {"agent": agent_name,
                "generation": definition.generation,
                "metrics": metrics,
                "task_id": run.id}

    def agent_evolution(self, session: Session, agent_name: str
                        ) -> dict[str, Any]:
        factory = self._agent_factory(session)
        definition = factory.get(agent_name)
        if definition is None:
            raise InvalidRequest(f"Unknown agent: {agent_name}")
        evolution = self._agent_evolution(session)
        return {"agent": agent_name,
                "generation": definition.generation,
                "metrics": evolution.snapshot(agent_name),
                "note": "Metrics come from real recorded run outcomes; "
                        "no outcomes recorded means no metrics."}



    # -- agent execution (A51) ------------------------------------------------------------------

    def _agent_runner(self, session: Session):
        from forge.agents.runner import AgentRunner

        if not hasattr(self, "_agent_runners"):
            self._agent_runners: dict[str, Any] = {}
        runner = self._agent_runners.get(session.id)
        if runner is None:
            runner = AgentRunner(
                session.id,
                executor_builder=lambda role: self._build_role_executor(
                    session, role))
            self._agent_runners[session.id] = runner
        return runner

    def _build_role_executor(self, session: Session, role: str,
                             run_id: str = ""):
        from forge.agents.coder import CoderAgent
        from forge.agents.execution import CallableAgentExecutor
        from forge.agents.requirements import TaskRequirementExtractor
        from forge.runtime.defaults import create_default_runtime
        from forge.security.permissions import (OperationMode,
                                                PermissionManager)

        project = self.get_project(session.project_id)
        root = str(project.root)
        permissions = PermissionManager(
            mode=OperationMode.ASSISTED, policy=self.policy,
            store=self.approval_store, agent="forge-agent-run",
            audit=self.audit)
        if role == "coding":
            runtime = create_default_runtime(permissions, root)
            return CoderAgent(runtime=runtime, root=root, fabric=self.fabric,
                              approval_store=self.approval_store,
                              approval_callback=self
                              ._agent_run_approval_callback(session,
                                                            run_id))
        if role == "planning":
            extractor = TaskRequirementExtractor()

            def planner_worker(request):
                import json as _json

                requirements = extractor.extract(
                    request.task.description)
                return _json.dumps(
                    {"capabilities": list(requirements.capabilities),
                     "requirement":
                         request.task.description[:200]},
                    default=str)

            return CallableAgentExecutor("planner", planner_worker)
        if role == "research":
            def researcher_worker(request):
                import json as _json
                import os as _os

                del request
                python_files: list[str] = []

                def bounded_files():
                    for dirpath, dirnames, filenames in _os.walk(root):
                        dirnames[:] = [name for name in dirnames
                                       if name not in (
                                           ".git", ".forge", "__pycache__",
                                           ".venv", "node_modules",
                                           ".arena")]
                        for name in sorted(filenames):
                            if name.endswith(".py"):
                                python_files.append(_os.path.relpath(
                                    _os.path.join(dirpath, name), root))
                                if len(python_files) >= 200:
                                    return

                bounded_files()
                test_files = [path for path in python_files
                              if "test" in path]
                return _json.dumps(
                    {"python_files": len(python_files),
                     "test_files": len(test_files),
                     "sample": python_files[:20]})

            return CallableAgentExecutor("researcher", researcher_worker)
        return None

    def _file_agent_run_approval(self, session: Session, name: str,
                                 role: str) -> dict[str, Any]:
        from forge.security.approvals import ApprovalRequest

        request = self.approval_store.submit(ApprovalRequest(
            agent="forge-agent-run", resource=Resource.AGENT,
            operation="execute", scopes=(name,),
            task_id=session.active_task or session.id,
            reason=f"Direct agent run {name} (role={role})",
            consequences="The bound executor for this agent runs "
                         "inside the same policy gates as built-in "
                         "agents."))
        self._audit(session.actor, "agents", "run", False,
                    task_id=session.active_task or session.id,
                    reason="approval required")
        return {"allowed": False, "approval_required": True,
                "approval_request_id": request.id, "run": None}

    def _agent_run_approval_callback(self, session: Session,
                                      run_id: str) -> Callable[[Any], str]:
        from forge.control.approvals import ApprovalError

        adapter = type("ApprovalRun", (), {
            "id": run_id,
            "project_id": session.project_id})()
        control = type("Control", (), {"cancel_requested": False})()

        def callback(query: Any) -> str:
            try:
                filed = self.approvals.file_query(
                    query, adapter, model="", provider="")
            except ApprovalError:
                return ""
            tokens = [
                self._wait_approval_token(
                    request, session.id, control,
                    self.config.approval_timeout)
                for request in filed]
            return tokens[0] if len(tokens) == 1 else ""

        return callback

    def agent_run(self, session: Session, name: str, requirement: str, *,
                  approval_id: str = "") -> dict[str, Any]:
        """Run a defined agent through its bound executor, policy-gated.

        The gate decision is synchronous; execution is dispatched to a
        worker thread (change-set approvals wait there, exactly like
        task runs) and the result is polled via ``agent_run_result``.
        """
        from uuid import uuid4

        from forge.security.approvals import enforce_with_token
        from forge.security.policy import (PermissionEvaluation,
                                           PermissionRequest)

        if not isinstance(requirement, str) or not requirement.strip() \
                or len(requirement) > 4000:
            raise InvalidRequest("Requirement must be 1-4000 characters.")
        factory = self._agent_factory(session)
        definition = factory.get(name)
        if definition is None:
            raise InvalidRequest(f"Unknown agent: {name}")
        if definition.status != "active":
            raise InvalidRequest(
                f"Agent {name} is {definition.status}; only active "
                "agents can run")
        governor = self._agent_governor()
        allowed, quota_reason = governor.check(name)
        if not allowed:
            self._audit(session.actor, "agents", "run", False,
                        task_id=session.active_task or session.id,
                        reason=quota_reason)
            raise InvalidRequest(quota_reason)
        permission = PermissionRequest(
            agent="forge-agent-run", resource=Resource.AGENT,
            operation="execute", scope=name,
            task_id=session.active_task or session.id,
            reason=f"direct agent run {name} (role={definition.role})",
            details=(("agent", name), ("role", definition.role)))
        policy = self.policy if self.policy is not None else PermissionPolicy()
        evaluation = policy.evaluate(permission)
        self.audit.record_evaluation(permission, evaluation)
        if evaluation.decision == PolicyDecision.REQUIRE_APPROVAL \
                and approval_id:
            allowed, _reason = enforce_with_token(
                self.approval_store, approval_id, permission)
            if allowed:
                evaluation = PermissionEvaluation(
                    decision=PolicyDecision.ALLOW, reason=_reason,
                    risk=permission.risk, scope=permission.scope,
                    request_id=permission.request_id)
            else:
                return self._file_agent_run_approval(
                    session, name, definition.role)
        if evaluation.decision == PolicyDecision.REQUIRE_APPROVAL:
            return self._file_agent_run_approval(session, name,
                                                 definition.role)
        if evaluation.decision != PolicyDecision.ALLOW:
            self._audit(session.actor, "agents", "run", False,
                        task_id=session.active_task or session.id,
                        reason=evaluation.reason)
            return {"allowed": False, "approval_required": False,
                    "approval_request_id": "", "run_id": "",
                    "reason": evaluation.reason or "denied by policy"}
        from forge.agents.runner import AgentRunner

        run_id = uuid4().hex[:12]
        record = Run(
            id=f"agent-run-{run_id}", project_id=session.project_id,
            requirement=requirement, status=RunStatus.RUNNING,
            stage="agent-run", version=1, mode=session.profile,
            actor=session.actor, created_at=time.time(),
            updated_at=time.time(), started_at=time.time())
        self.runs.create(record)
        runner = AgentRunner(
            session.id,
            executor_builder=lambda role, run_id=run_id:
            self._build_role_executor(session, role, run_id=run_id))
        if not hasattr(self, "_agent_run_results"):
            self._agent_run_results: dict[tuple, dict[str, Any]] = {}
        if not hasattr(self, "_agent_run_logs"):
            self._agent_run_logs: dict[str, list[dict[str, Any]]] = {}

        def worker():
            try:
                result = runner.run(definition, requirement,
                                    run_id=run_id)
                self.runs.mutate(
                    record.id,
                    status=(RunStatus.SUCCEEDED if result.success
                            else RunStatus.FAILED),
                    stage="completed", finished_at=time.time(),
                    files_json=json.dumps(list(result.files)),
                    report_json=json.dumps(
                        {"output": result.output[:2000],
                         "error": result.error}))
                self._agent_run_results[(session.id, run_id)] = {
                    "status": "finished", "run": result.to_dict()}
                self._agent_run_logs.setdefault(
                    session.id, []).append(result.to_dict())
                self._agent_run_logs[session.id] = \
                    self._agent_run_logs[session.id][-20:]
                self._audit(session.actor, "agents", "run", True,
                            task_id=record.id,
                            reason=f"{name} role={result.role} "
                                   f"success={result.success} "
                                   f"elapsed={result.elapsed_ms}ms")
            except Exception as exc:  # bounded, audited, never silent
                try:
                    self._metrics().incr("agent_runs.failed")
                except Exception:
                    pass
                self.runs.mutate(record.id, status=RunStatus.FAILED,
                                 stage="failed", finished_at=time.time(),
                                 error=str(exc)[:500])
                self._agent_run_results[(session.id, run_id)] = {
                    "status": "failed", "error": str(exc)[:500]}
                self._agent_run_logs.setdefault(session.id, []).append({
                    "run_id": run_id, "agent": name,
                    "role": definition.role, "success": False,
                    "output": "", "error": str(exc)[:500],
                    "elapsed_ms": 0.0, "files": [], "at": time.time()})
                self._agent_run_logs[session.id] = \
                    self._agent_run_logs[session.id][-20:]
                try:
                    self._metrics().incr("agent_runs.succeeded")
                    self._metrics().observe(
                        "agent_run.duration_ms",
                        (result.elapsed_ms or 0.0) / 1000.0)
                except Exception:
                    pass
                try:
                    self._failure_ledger().record(
                        "agent", str(exc), task_id=record.id,
                        actor=session.actor)
                except Exception:
                    pass
                self._audit(session.actor, "agents", "run", False,
                            task_id=record.id,
                            reason=f"{name} error={str(exc)[:300]}")

        def tracked_worker():
            try:
                worker()
            finally:
                self._agent_governor().end(name)

        governor.begin(name)
        if self._executor is not None:
            self._executor.submit(tracked_worker)
        else:
            tracked_worker()
        return {"allowed": True, "approval_required": False,
                "approval_request_id": "", "run_id": run_id,
                "status": "queued", "task_id": record.id}

    def agent_run_result(self, session: Session, name: str, run_id: str
                         ) -> dict[str, Any]:
        factory = self._agent_factory(session)
        if factory.get(name) is None:
            raise InvalidRequest(f"Unknown agent: {name}")
        results = getattr(self, "_agent_run_results", {})
        entry = results.get((session.id, run_id))
        if entry is None:
            return {"status": "pending", "run_id": run_id}
        return {"run_id": run_id, **entry}

    def agent_runs(self, session: Session, name: str
                   ) -> dict[str, Any]:
        factory = self._agent_factory(session)
        if factory.get(name) is None:
            raise InvalidRequest(f"Unknown agent: {name}")
        logs = getattr(self, "_agent_run_logs", {}).get(session.id, [])
        return {"agent": name,
                "runs": [entry for entry in logs
                         if entry["agent"] == name]}

    def list_agent_run_approvals(self, session: Session
                                 ) -> list[dict[str, Any]]:
        visible = []
        for request in self.approval_store.pending():
            if request.agent == "forge-agent-run" \
                    and request.task_id in (session.id, session.active_task):
                visible.append(request.to_dict())
        return visible

    def decide_agent_run_approval(self, session: Session, approval_id: str,
                                  approved: bool) -> dict[str, Any]:
        request = self.approval_store.get_request(approval_id)
        if request is None or request.agent != "forge-agent-run" \
                or request.task_id not in (session.id, session.active_task):
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
        self._audit(session.actor, "agents",
                    "approve" if approved else "deny", True,
                    task_id=request.task_id,
                    reason=f"agent-run approval {approval_id}")
        return {"approval": decided.to_dict(), "token_id": token_id}



    # -- agent teams (A52) -----------------------------------------------------------------------

    def _team_registry(self, session: Session):
        from forge.agents.teams import TeamRegistry

        if not hasattr(self, "_team_registries"):
            self._team_registries: dict[str, Any] = {}
        registry = self._team_registries.get(session.id)
        if registry is None:
            registry = TeamRegistry(session.id)
            self._team_registries[session.id] = registry
        return registry

    def team_create(self, session: Session, name: str,
                    members: list[str]) -> dict[str, Any]:
        factory = self._agent_factory(session)
        registry = self._team_registry(session)
        try:
            team = registry.create(
                name, members, agent_lookup=factory.get,
                created_by=session.actor)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "teams", "create", True,
                    task_id=session.active_task or session.id,
                    reason=f"{team.name} members={list(team.members)}")
        return team.to_dict()

    def team_list(self, session: Session) -> dict[str, Any]:
        return {"teams": [team.to_dict() for team in
                          self._team_registry(session).list()]}

    def team_execute(self, session: Session, team_id: str,
                     requirement: str, *,
                     approval_id: str = "") -> dict[str, Any]:
        """Run a team sequentially.

        Each member executes through ``agent_run`` — so every member
        inherits the full AGENT/execute gate (DENY fail-closed,
        approval round trips) and runs as its own real recorded run.
        """
        from uuid import uuid4

        if not isinstance(requirement, str) or not requirement.strip() \
                or len(requirement) > 4000:
            raise InvalidRequest("Requirement must be 1-4000 characters.")
        registry = self._team_registry(session)
        team = registry.get(team_id)
        if team is None:
            raise InvalidRequest(f"Unknown team: {team_id}")
        run_id = uuid4().hex[:12]
        record = Run(
            id=f"team-run-{run_id}", project_id=session.project_id,
            requirement=requirement, status=RunStatus.RUNNING,
            stage="team-run", version=1, mode=session.profile,
            actor=session.actor, created_at=time.time(),
            updated_at=time.time(), started_at=time.time())
        self.runs.create(record)
        team.status = "running"
        if not hasattr(self, "_team_run_results"):
            self._team_run_results: dict[tuple, dict[str, Any]] = {}
        member_names = list(team.members)

        def worker():
            results: list[dict[str, Any]] = []
            context = ""
            success = True
            try:
                for name in member_names:
                    requirement_with_context = requirement
                    if context:
                        requirement_with_context = (
                            f"{requirement} [prior step summary: "
                            f"{context}]")[:4000]
                    dispatched = self.agent_run(
                        session, name, requirement_with_context,
                        approval_id=approval_id)
                    if not dispatched["allowed"]:
                        results.append({"agent": name, "success": False,
                                        "error": dispatched.get(
                                            "reason", "denied by policy")})
                        success = False
                        break
                    deadline = time.time() + max(
                        60.0, self.config.approval_timeout + 30.0)
                    final = None
                    while time.time() < deadline:
                        state = self.agent_run_result(
                            session, name, dispatched["run_id"])
                        if state["status"] == "finished":
                            final = state["run"]
                            break
                        if state["status"] == "failed":
                            final = {"agent": name, "success": False,
                                     "error": state["error"],
                                     "output": ""}
                            break
                        time.sleep(0.1)
                    if final is None:
                        final = {"agent": name, "success": False,
                                 "error": "member run timed out",
                                 "output": ""}
                    results.append(final)
                    if not final.get("success"):
                        success = False
                        break
                    context = (final.get("output") or
                               final.get("error") or "")[:600]
                self.runs.mutate(
                    record.id,
                    status=(RunStatus.SUCCEEDED if success
                            else RunStatus.FAILED),
                    stage="completed", finished_at=time.time(),
                    report_json=json.dumps({"results": results}))
                self._team_run_results[(session.id, run_id)] = {
                    "status": "finished", "success": success,
                    "results": results}
                team.status = "succeeded" if success else "failed"
                self._audit(session.actor, "teams", "execute", True,
                            task_id=record.id,
                            reason=f"{team.name} "
                                   f"members={len(results)} "
                                   f"success={success}")
            except Exception as exc:  # bounded, audited, never silent
                self.runs.mutate(record.id, status=RunStatus.FAILED,
                                 stage="failed",
                                 finished_at=time.time(),
                                 error=str(exc)[:500])
                self._team_run_results[(session.id, run_id)] = {
                    "status": "failed", "error": str(exc)[:500]}
                team.status = "failed"
                self._audit(session.actor, "teams", "execute", False,
                            task_id=record.id,
                            reason=f"{team.name} "
                                   f"error={str(exc)[:300]}")

        if self._executor is not None:
            self._executor.submit(worker)
        else:
            worker()
        return {"allowed": True, "approval_required": False,
                "approval_request_id": "", "run_id": run_id,
                "status": "queued", "task_id": record.id}

    def team_run_result(self, session: Session, team_id: str,
                        run_id: str) -> dict[str, Any]:
        registry = self._team_registry(session)
        if registry.get(team_id) is None:
            raise InvalidRequest(f"Unknown team: {team_id}")
        results = getattr(self, "_team_run_results", {})
        entry = results.get((session.id, run_id))
        if entry is None:
            return {"status": "pending", "run_id": run_id}
        return {"run_id": run_id, **entry}



    # -- agent memory (A53) ----------------------------------------------------------------------

    def _agent_memory_store(self):
        from forge.agents.memory import AgentMemoryStore

        if getattr(self, "_agent_memories", None) is None:
            self._agent_memories = AgentMemoryStore(self._db)
        return self._agent_memories

    def _agent_memory_gate(self, session: Session, name: str,
                           operation: str) -> bool:
        from forge.security.policy import PermissionRequest

        permission = PermissionRequest(
            agent="forge-agent-memory", resource=Resource.MEMORY,
            operation=operation, scope=f"agent:{name}",
            task_id=session.active_task or session.id,
            reason=f"agent memory {operation} for {name}",
            details=(("agent", name),))
        policy = self.policy if self.policy is not None else PermissionPolicy()
        evaluation = policy.evaluate(permission)
        self.audit.record_evaluation(permission, evaluation)
        return evaluation.decision == PolicyDecision.ALLOW

    def agent_memory_set(self, session: Session, name: str, key: str,
                         value: str) -> dict[str, Any]:
        factory = self._agent_factory(session)
        if factory.get(name) is None:
            raise InvalidRequest(f"Unknown agent: {name}")
        if not self._agent_memory_gate(session, name, "write"):
            return {"allowed": False,
                    "reason": "denied by memory policy"}
        try:
            entry = self._agent_memory_store().set(name, key, value)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "agents", "memory_set", True,
                    task_id=session.active_task or session.id,
                    reason=f"{name} key={entry['key']}")
        return {"allowed": True, "entry": entry}

    def agent_memory_get(self, session: Session, name: str,
                         key: str) -> dict[str, Any]:
        factory = self._agent_factory(session)
        if factory.get(name) is None:
            raise InvalidRequest(f"Unknown agent: {name}")
        if not self._agent_memory_gate(session, name, "read"):
            return {"allowed": False,
                    "reason": "denied by memory policy"}
        entry = self._agent_memory_store().get(name, key)
        return {"allowed": True, "entry": entry}

    def agent_memory_list(self, session: Session, name: str
                          ) -> dict[str, Any]:
        factory = self._agent_factory(session)
        if factory.get(name) is None:
            raise InvalidRequest(f"Unknown agent: {name}")
        if not self._agent_memory_gate(session, name, "read"):
            return {"allowed": False,
                    "reason": "denied by memory policy"}
        return {"allowed": True,
                "entries": self._agent_memory_store().list(name)}

    def agent_memory_delete(self, session: Session, name: str,
                            key: str) -> dict[str, Any]:
        factory = self._agent_factory(session)
        if factory.get(name) is None:
            raise InvalidRequest(f"Unknown agent: {name}")
        if not self._agent_memory_gate(session, name, "delete"):
            return {"allowed": False,
                    "reason": "denied by memory policy"}
        deleted = self._agent_memory_store().delete(name, key)
        if deleted:
            self._audit(session.actor, "agents", "memory_delete", True,
                        task_id=session.active_task or session.id,
                        reason=f"{name} key={key}")
        return {"allowed": True, "deleted": deleted}



    # -- agent skills (A54) ------------------------------------------------------------------------

    def _skill_registry(self, session: Session):
        from forge.agents.skills import SkillRegistry

        if not hasattr(self, "_skill_registries"):
            self._skill_registries: dict[str, Any] = {}
        registry = self._skill_registries.get(session.id)
        if registry is None:
            registry = SkillRegistry(session.id)
            self._skill_registries[session.id] = registry
        return registry

    def skill_create(self, session: Session, name: str,
                     capability: str, *, description: str = "",
                     version: str = "1.0.0") -> dict[str, Any]:
        registry = self._skill_registry(session)
        try:
            skill = registry.create(
                name, capability, description=description,
                version=version, created_by=session.actor)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "skills", "create", True,
                    task_id=session.active_task or session.id,
                    reason=f"{skill.name} capability={skill.capability}")
        return skill.to_dict()

    def skill_list(self, session: Session) -> dict[str, Any]:
        return {"skills": [skill.to_dict() for skill in
                           self._skill_registry(session).list()]}

    def agent_attach_skill(self, session: Session, name: str,
                           skill_name: str) -> dict[str, Any]:
        from forge.agents.skills import MAX_ATTACHED

        factory = self._agent_factory(session)
        definition = factory.get(name)
        if definition is None:
            raise InvalidRequest(f"Unknown agent: {name}")
        skill = self._skill_registry(session).get(skill_name)
        if skill is None:
            raise InvalidRequest(f"Unknown skill: {skill_name}")
        if len(definition.skills) >= MAX_ATTACHED:
            raise InvalidRequest(f"skill limit reached ({MAX_ATTACHED})")
        if skill_name in definition.skills:
            raise InvalidRequest(
                f"Skill {skill_name!r} already attached to {name}")
        base = definition.base_capabilities or definition.capabilities
        caps = tuple(dict.fromkeys(base + (skill.capability,)))
        updated = factory.update(
            name, capabilities=caps,
            description=definition.description)
        updated.skills = tuple(sorted(definition.skills + (skill.name,)))
        updated.base_capabilities = base
        self._audit(session.actor, "skills", "attach", True,
                    task_id=session.active_task or session.id,
                    reason=f"{name} <- {skill.name}")
        return updated.to_dict()

    def agent_detach_skill(self, session: Session, name: str,
                           skill_name: str) -> dict[str, Any]:
        factory = self._agent_factory(session)
        definition = factory.get(name)
        if definition is None:
            raise InvalidRequest(f"Unknown agent: {name}")
        if skill_name not in definition.skills:
            raise InvalidRequest(
                f"Skill {skill_name!r} is not attached to {name}")
        remaining = tuple(skill for skill in definition.skills
                          if skill != skill_name)
        registry = self._skill_registry(session)
        base = definition.base_capabilities or definition.capabilities
        skill_caps = tuple(dict.fromkeys(
            registry.get(skill).capability for skill in remaining
            if registry.get(skill) is not None))
        caps = tuple(dict.fromkeys(base + skill_caps))
        updated = factory.update(name, capabilities=caps,
                                 description=definition.description)
        updated.skills = remaining
        updated.base_capabilities = base
        self._audit(session.actor, "skills", "detach", True,
                    task_id=session.active_task or session.id,
                    reason=f"{name} -/ {skill_name}")
        return updated.to_dict()














    # -- cockpit navigation (A68) ---------------------------------------------------------------------

    def command_shortcuts(self, session: Session) -> dict[str, Any]:
        """Canonical keyboard navigation catalog for the cockpit."""
        from forge.cockpit_shortcuts import (shortcut_payload,
                                             validate_shortcuts)

        payload = shortcut_payload()
        validate_shortcuts(payload["entries"])
        return payload


    # -- command palette (A67) ------------------------------------------------------------------------

    def command_palette(self, session: Session) -> dict[str, Any]:
        """Canonical cockpit palette: views and safe quick actions."""
        from forge.cockpit_palette import palette_payload, validate_entries

        payload = palette_payload()
        validate_entries(payload["entries"])
        return payload


    # -- plugin SDK (A66) ----------------------------------------------------------------------------

    def _plugin_registry(self, session: Session):
        from forge.plugins.registry import PluginRegistry

        if not hasattr(self, "_plugin_registries"):
            self._plugin_registries: dict[str, Any] = {}
        registry = self._plugin_registries.get(session.id)
        if registry is None:
            registry = PluginRegistry(session.id)
            self._plugin_registries[session.id] = registry
        return registry

    def _real_capabilities(self) -> set:
        real: set = set()
        for entry in self.agent_catalog():
            if entry.get("real"):
                real.update(entry.get("capabilities", []))
        try:
            for model in self.fabric.registry.list():
                real.update(getattr(model, "capabilities", ()) or ())
        except Exception:
            pass
        return real

    def plugin_install(self, session: Session,
                       manifest: Any) -> dict[str, Any]:
        registry = self._plugin_registry(session)
        try:
            plugin = registry.install(manifest,
                                      installed_by=session.actor)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "plugins", "install", True,
                    task_id=session.active_task or session.id,
                    reason=f"{plugin.manifest['name']} "
                           f"kind={plugin.manifest['kind']} "
                           f"caps={','.join(plugin.manifest['capabilities'])}")
        return plugin.to_dict()

    def plugin_list(self, session: Session) -> dict[str, Any]:
        return {"plugins": [plugin.to_dict() for plugin in
                            self._plugin_registry(session).list()]}

    def plugin_status(self, session: Session, plugin_id: str
                      ) -> dict[str, Any]:
        from forge.plugins.registry import bind_capabilities

        registry = self._plugin_registry(session)
        try:
            plugin = registry.get(plugin_id)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        real = self._real_capabilities()
        return bind_capabilities(plugin, lambda cap: cap in real)

    def plugin_remove(self, session: Session, plugin_id: str
                      ) -> dict[str, Any]:
        registry = self._plugin_registry(session)
        try:
            plugin = registry.remove(plugin_id)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "plugins", "remove", True,
                    task_id=session.active_task or session.id,
                    reason=f"{plugin.manifest['name']} {plugin_id}")
        return plugin.to_dict()


    # -- backup & recovery (A65) ----------------------------------------------------------------------

    def _backup_manager(self):
        from forge.backup.manager import BackupManager

        if not hasattr(self, "_backups"):
            self._backups = BackupManager(
                self._db, self.config.db_path, self.config.projects,
                self._db.path.parent / "backups")
        return self._backups

    def backup_create(self, session: Session, label: str
                      ) -> dict[str, Any]:
        try:
            record = self._backup_manager().create(label, session.actor)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "backup", "create", True,
                    task_id=session.active_task or session.id,
                    reason=f"{label} files={record['files_count']}")
        return record

    def backup_verify(self, session: Session, backup_id: str
                      ) -> dict[str, Any]:
        try:
            report = self._backup_manager().verify(backup_id)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "backup", "verify", True,
                    task_id=session.active_task or session.id,
                    reason=f"{backup_id} status={report['status']}")
        return report

    def backup_restore(self, session: Session, backup_id: str
                       ) -> dict[str, Any]:
        if self.running:
            raise InvalidRequest(
                "restore requires a stopped plane; stop the plane first")
        try:
            report = self._backup_manager().restore(
                backup_id, stopped=True, actor=session.actor)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "backup", "restore", True,
                    task_id=session.active_task or session.id,
                    reason=backup_id)
        return report

    def backup_list(self, session: Session) -> dict[str, Any]:
        return {"backups": self._backup_manager().list()}

    def backup_get(self, session: Session, backup_id: str
                   ) -> dict[str, Any]:
        try:
            return self._backup_manager().get(backup_id)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc


    # -- deployment (A64) ----------------------------------------------------------------------------

    def _deployment_manager(self, session: Session):
        from forge.deployment.manager import DeploymentManager

        key = f"deployment:{session.project_id}"
        if not hasattr(self, "_deployment_managers"):
            self._deployment_managers: dict[str, Any] = {}
        manager = self._deployment_managers.get(key)
        if manager is None:
            project = self.get_project(session.project_id)
            manager = DeploymentManager(
                self._db, session.project_id, project.root,
                self._db.path.parent / "deployments")
            self._deployment_managers[key] = manager
        return manager

    def deployment_create(self, session: Session, name: str,
                          version: str) -> dict[str, Any]:
        try:
            record = self._deployment_manager(session).create(
                name, version, session.actor)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "deployment", "create", True,
                    task_id=session.active_task or session.id,
                    reason=f"{name} {version}")
        return record

    def deployment_build(self, session: Session, deployment_id: str
                         ) -> dict[str, Any]:
        try:
            record = self._deployment_manager(session).build(
                deployment_id, session.actor)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "deployment", "build", True,
                    task_id=session.active_task or session.id,
                    reason=f"{deployment_id} files={record['files_count']}")
        return record

    def deployment_deploy(self, session: Session, deployment_id: str,
                          target: str) -> dict[str, Any]:
        if not target or not isinstance(target, str):
            raise InvalidRequest("target must be a non-empty path string")
        try:
            record = self._deployment_manager(session).deploy(
                deployment_id, target, session.actor)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "deployment", "deploy", True,
                    task_id=session.active_task or session.id,
                    reason=f"{deployment_id} target={record['target']}")
        return record

    def deployment_rollback(self, session: Session, deployment_id: str
                            ) -> dict[str, Any]:
        try:
            record = self._deployment_manager(session).rollback(
                deployment_id, session.actor)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "deployment", "rollback", True,
                    task_id=session.active_task or session.id,
                    reason=f"{deployment_id} -> "
                           f"build_index={record['build_index']}")
        return record

    def deployment_list(self, session: Session) -> dict[str, Any]:
        return {"deployments":
                self._deployment_manager(session).list()}

    def deployment_get(self, session: Session, deployment_id: str
                       ) -> dict[str, Any]:
        try:
            return self._deployment_manager(session).get(deployment_id)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc


    # -- performance (A63) ---------------------------------------------------------------------------

    def performance_summary(self, session: Session, limit: int = 200
                            ) -> dict[str, Any]:
        from forge.performance.profiler import (MAX_SUMMARY_RUNS,
                                                performance_summary)

        if limit < 1 or limit > MAX_SUMMARY_RUNS:
            raise InvalidRequest(f"limit must be 1-{MAX_SUMMARY_RUNS}")
        rows, _total = self.runs.list_for_project(
            session.project_id, limit=limit)
        return performance_summary(rows)

    def performance_run(self, session: Session, run_id: str
                        ) -> dict[str, Any]:
        from forge.performance.profiler import run_profile

        run = self.runs.get(run_id)
        # Cross-project ids map to NOT_FOUND: existence must not leak.
        if run is None or run.project_id != session.project_id:
            raise TaskNotFound(f"Unknown task: {run_id!r}")
        return run_profile(run)


    # -- observability (A62) -------------------------------------------------------------------------

    def _metrics(self):
        from forge.observability.metrics import MetricsRegistry

        if not hasattr(self, "_metrics_registry"):
            self._metrics_registry = MetricsRegistry()
        return self._metrics_registry

    def observability_snapshot(self, session: Session) -> dict[str, Any]:
        """Counters, latency summaries, and gauges from real events."""
        registry = self._metrics()
        data = registry.snapshot()
        try:
            factory = self._agent_factory(session)
            agents_defined = len(factory.list())
        except Exception:
            agents_defined = 0
        try:
            teams_defined = len(self._team_registry(session).list())
        except Exception:
            teams_defined = 0
        try:
            ledger_stats = self._failure_ledger().stats()
        except Exception:
            ledger_stats = {}
        active_runs = 0
        try:
            for project in self.projects.values():
                rows, _total = self.runs.list_for_project(project.id)
                active_runs += sum(
                    1 for row in rows if row.status not in
                    ("SUCCEEDED", "FAILED", "CANCELLED", "ROLLED_BACK"))
        except Exception:
            pass
        data["gauges"] = {
            "active_sessions": self.sessions.count_active(),
            "agents_defined": agents_defined,
            "teams_defined": teams_defined,
            "active_runs": active_runs,
            "distinct_failures": ledger_stats.get("distinct_keys", 0),
            "total_failure_events": ledger_stats.get("total_events", 0),
        }
        return data


    # -- security hardening (A61) --------------------------------------------------------------------

    def hardening_report(self, session: Session) -> dict[str, Any]:
        """Bounded, read-only security audit of this plane."""
        from forge.security.hardening import run_hardening_report

        report = run_hardening_report(
            self.policy if self.policy is not None else PermissionPolicy(),
            self.sessions, self.projects)
        self._audit(session.actor, "security", "hardening", True,
                    task_id=session.active_task or session.id,
                    reason=f"overall={report['overall']}")
        return report


    # -- model benchmarking (A60) --------------------------------------------------------------------

    def _benchmark_store(self):
        from forge.benchmark.harness import BenchmarkStore

        if not hasattr(self, "_benchmarks"):
            self._benchmarks = BenchmarkStore(self._db)
        return self._benchmarks

    def benchmark_run(self, session: Session,
                      models: list[str] | None = None
                      ) -> dict[str, Any]:
        from forge.benchmark.harness import MAX_MODELS, run_benchmark

        if models is not None:
            if not isinstance(models, list) or len(models) > MAX_MODELS:
                raise InvalidRequest(
                    f"models must be a list of at most {MAX_MODELS} names")
            for name in models:
                if not isinstance(name, str) or not name.strip():
                    raise InvalidRequest("model names must be strings")
        results, summary = run_benchmark(self.fabric, models or None)
        if models and summary["unknown_models"]:
            raise InvalidRequest(
                f"Unknown model(s): {', '.join(summary['unknown_models'])}")
        store = self._benchmark_store()
        recorded = [store.record(entry, session.actor)
                    for entry in results]
        self._audit(session.actor, "benchmark", "run", True,
                    task_id=session.active_task or session.id,
                    reason=f"models={len(recorded)} "
                           f"passed={summary['passed']}/"
                           f"{summary['total']}")
        return {"benchmarks": recorded, "summary": summary}

    def benchmark_history(self, session: Session, limit: int = 20
                          ) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise InvalidRequest("limit must be 1-100")
        return {"benchmarks": self._benchmark_store().history(limit)}


    # -- failure learning (A59) ----------------------------------------------------------------------

    def _failure_ledger(self):
        from forge.learning.failures import FailureLedger

        if not hasattr(self, "_failures"):
            self._failures = FailureLedger(self._db)
        return self._failures

    def record_failure(self, session: Session, category: str,
                       error: str) -> dict[str, Any]:
        try:
            event = self._failure_ledger().record(
                category, error, task_id=session.active_task or "",
                actor=session.actor)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "learning", "record", True,
                    task_id=session.active_task or session.id,
                    reason=f"{category} "
                           f"fingerprint={event['fingerprint'][:40]}")
        return event

    def failure_lessons(self, session: Session, limit: int = 5
                        ) -> dict[str, Any]:
        if limit < 1 or limit > 50:
            raise InvalidRequest("limit must be 1-50")
        ledger = self._failure_ledger()
        return {"lessons": ledger.lessons(limit),
                "top": ledger.top(limit),
                "stats": ledger.stats()}


    # -- agent governance (A57) ----------------------------------------------------------------------

    def _agent_governor(self):
        from forge.agents.governance import AgentGovernor

        if not hasattr(self, "_governor"):
            self._governor = AgentGovernor()
        return self._governor

    def agent_set_limits(self, session: Session, name: str, *,
                         max_runs_per_hour: int = 60,
                         max_concurrent: int = 2) -> dict[str, Any]:
        factory = self._agent_factory(session)
        if factory.get(name) is None:
            raise InvalidRequest(f"Unknown agent: {name}")
        try:
            limits = self._agent_governor().set_limits(
                name, max_runs_per_hour=max_runs_per_hour,
                max_concurrent=max_concurrent)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "agents", "limits", True,
                    task_id=session.active_task or session.id,
                    reason=f"{name} {limits}")
        return limits

    def agent_limits(self, session: Session, name: str) -> dict[str, Any]:
        if self._agent_factory(session).get(name) is None:
            raise InvalidRequest(f"Unknown agent: {name}")
        return self._agent_governor().limits(name)



    # -- agent self-development (A58) ---------------------------------------------------------------

    def _selfdev_ledger(self, session: Session):
        from forge.agents.selfdev import SelfDevLedger

        if not hasattr(self, "_selfdev_ledgers"):
            self._selfdev_ledgers: dict[str, Any] = {}
        ledger = self._selfdev_ledgers.get(session.id)
        if ledger is None:
            ledger = SelfDevLedger(session.id)
            self._selfdev_ledgers[session.id] = ledger
        return ledger

    def selfdev_analyze(self, session: Session, name: str
                        ) -> dict[str, Any]:
        factory = self._agent_factory(session)
        if factory.get(name) is None:
            raise InvalidRequest(f"Unknown agent: {name}")
        logs = getattr(self, "_agent_run_logs", {}).get(session.id, [])
        ledger = self._selfdev_ledger(session)
        proposal = ledger.analyze(name, logs)
        failures = [entry for entry in logs
                    if entry.get("agent") == name
                    and not entry.get("success", True)]
        self._audit(session.actor, "selfdev", "analyze", True,
                    task_id=session.active_task or session.id,
                    reason=f"{name} kind={proposal.kind} "
                           f"failures={len(failures)}")
        return {"proposal": proposal.to_dict(),
                "failures_seen": len(failures)}

    def selfdev_apply(self, session: Session, name: str,
                      proposal_id: str) -> dict[str, Any]:
        from forge.agents.selfdev import (MAX_APPLIED_PER_AGENT,
                                          MAX_APPLIED_PER_SESSION)

        factory = self._agent_factory(session)
        definition = factory.get(name)
        if definition is None:
            raise InvalidRequest(f"Unknown agent: {name}")
        ledger = self._selfdev_ledger(session)
        proposal = ledger.get(proposal_id)
        if proposal is None:
            raise InvalidRequest(f"Unknown proposal: {proposal_id}")
        if proposal.agent != name:
            raise InvalidRequest(
                "That proposal belongs to a different agent")
        if proposal.applied:
            raise InvalidRequest("Proposal already applied")
        if proposal.kind == "none":
            raise InvalidRequest(
                "This proposal is not actionable: "
                f"{proposal.reason[:200]}")
        if proposal.kind != "failure-note":
            raise InvalidRequest(
                f"Unsupported proposal kind: {proposal.kind}")
        if ledger.applied_per_session >= MAX_APPLIED_PER_SESSION:
            raise InvalidRequest(
                f"Session self-development budget exhausted "
                f"({MAX_APPLIED_PER_SESSION})")
        if ledger.applied_count(name) >= MAX_APPLIED_PER_AGENT:
            raise InvalidRequest(
                f"Agent {name} self-development budget exhausted "
                f"({MAX_APPLIED_PER_AGENT})")
        updated = factory.update(
            name,
            description=(definition.description + "\n[learned] "
                         + proposal.note).strip()[:500])
        updated.generation += 1
        metrics = dict(definition.metrics)
        metrics.update(proposal.metrics)
        updated.metrics = metrics
        ledger.mark_applied(proposal)
        self._audit(session.actor, "selfdev", "apply", True,
                    task_id=session.active_task or session.id,
                    reason=f"{name} <- {proposal.proposal_id} "
                           f"kind={proposal.kind}")
        return {"agent": updated.to_dict(),
                "proposal": proposal.to_dict()}

    def selfdev_ledger(self, session: Session, name: str
                       ) -> dict[str, Any]:
        factory = self._agent_factory(session)
        if factory.get(name) is None:
            raise InvalidRequest(f"Unknown agent: {name}")
        return {"agent": name,
                "entries": self._selfdev_ledger(session).entries(name)}


    # -- agent lifecycle (A55) ----------------------------------------------------------------------

    def agent_set_status(self, session: Session, name: str,
                         status: str) -> dict[str, Any]:
        """Transition an agent between active/paused/retired."""
        factory = self._agent_factory(session)
        definition = factory.get(name)
        if definition is None:
            raise InvalidRequest(f"Unknown agent: {name}")
        status = (status or "").strip().lower()
        if status not in ("active", "paused", "retired"):
            raise InvalidRequest(
                f"Unknown status {status!r}; expected active, paused, "
                "or retired")
        if definition.status == "retired":
            raise InvalidRequest("Retired agents cannot transition")
        if status == definition.status:
            raise InvalidRequest(f"Agent {name} is already {status}")
        if status == "active" and definition.status != "paused":
            raise InvalidRequest(
                f"Only paused agents can return to active "
                f"(current: {definition.status})")
        definition.status = status
        self._audit(session.actor, "agents", "status", True,
                    task_id=session.active_task or session.id,
                    reason=f"{name} -> {status}")
        return definition.to_dict()



    # -- agent packaging (A56) ----------------------------------------------------------------------

    def agent_export(self, session: Session, name: str
                     ) -> dict[str, Any]:
        from forge.agents.packaging import export_definition

        factory = self._agent_factory(session)
        definition = factory.get(name)
        if definition is None:
            raise InvalidRequest(f"Unknown agent: {name}")
        payload = export_definition(definition)
        self._audit(session.actor, "agents", "export", True,
                    task_id=session.active_task or session.id,
                    reason=name)
        return payload

    def agent_import(self, session: Session,
                     payload: Any) -> dict[str, Any]:
        from forge.agents.packaging import import_payload

        factory = self._agent_factory(session)
        registry = self._skill_registry(session)
        try:
            fields = import_payload(
                payload, skill_lookup=registry.get)
            definition = factory.create(
                fields["name"], fields["role"],
                fields["capabilities"],
                description=fields["description"],
                created_by="import:" + fields["created_by"],
                bind=False)
            definition.generation = fields["generation"]
            definition.metrics = dict(fields["metrics"])
            for skill in fields["skills"]:
                definition.skills = tuple(sorted(
                    definition.skills + (skill,)))
            skill_caps = tuple(dict.fromkeys(
                registry.get(skill).capability
                for skill in definition.skills
                if registry.get(skill) is not None))
            definition.base_capabilities = \
                tuple(fields["capabilities"])
            definition.capabilities = tuple(dict.fromkeys(
                tuple(fields["capabilities"]) + skill_caps))
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "agents", "import", True,
                    task_id=session.active_task or session.id,
                    reason=f"{fields['name']} "
                           f"dropped={fields['dropped_skills']}")
        return {"agent": definition.to_dict(),
                "dropped_skills": fields["dropped_skills"],
                "note": "Imported definitions are always unbound; "
                        "binding an executor is local."}


    # -- agent creation (A49) ----------------------------------------------------------------

    def _agent_factory(self, session: Session):
        from forge.agents.factory import AgentFactory

        if not hasattr(self, "_agent_factories"):
            self._agent_factories: dict[str, Any] = {}
        factory = self._agent_factories.get(session.id)
        if factory is None:
            factory = AgentFactory(session.id)
            self._agent_factories[session.id] = factory
        return factory

    def agent_create(self, session: Session, name: str, role: str,
                     capabilities: list[str] | tuple[str, ...], *,
                     description: str = "", bind: bool = False
                     ) -> dict[str, Any]:
        """Define a new agent; capabilities never grant power."""
        factory = self._agent_factory(session)
        try:
            definition = factory.create(
                name, role, tuple(capabilities), description=description,
                created_by=session.actor, bind=bind)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "agents", "create", True,
                    task_id=session.active_task or session.id,
                    reason=f"{name} role={role} "
                           f"real={definition.real}")
        return definition.to_dict()

    def agent_update(self, session: Session, name: str, *,
                     role: str = "",
                     capabilities: list[str] | None = None,
                     description: str | None = None
                     ) -> dict[str, Any]:
        factory = self._agent_factory(session)
        try:
            definition = factory.update(
                name, role=role,
                capabilities=tuple(capabilities)
                if capabilities is not None else None,
                description=description)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "agents", "update", True,
                    task_id=session.active_task or session.id,
                    reason=f"{name} real={definition.real}")
        return definition.to_dict()

    def agent_delete(self, session: Session, name: str
                     ) -> dict[str, Any]:
        factory = self._agent_factory(session)
        try:
            definition = factory.delete(name)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "agents", "delete", True,
                    task_id=session.active_task or session.id,
                    reason=name)
        return {"deleted": definition.to_dict()}

    def agent_definitions(self, session: Session) -> dict[str, Any]:
        factory = self._agent_factory(session)
        return {"agents": [definition.to_dict()
                           for definition in factory.list()]}


    # -- compute (A48) --------------------------------------------------------------------

    def _compute_engine(self, session: Session):
        from forge.compute.engine import ComputeEngine

        if not hasattr(self, "_compute_engines"):
            self._compute_engines: dict[str, Any] = {}
        engine = self._compute_engines.get(session.id)
        if engine is None:
            project = self.get_project(session.project_id)
            engine = ComputeEngine(
                project.root, max_cells=self.config.compute_max_cells,
                max_seconds=self.config.compute_max_seconds,
                cell_timeout=self.config.compute_cell_timeout)
            self._compute_engines[session.id] = engine
        return engine

    def _file_compute_approval(self, session: Session) -> dict[str, Any]:
        from forge.security.approvals import ApprovalRequest

        request = self.approval_store.submit(ApprovalRequest(
            agent="forge-compute", resource=Resource.TERMINAL,
            operation="execute", scopes=("python",),
            task_id=session.active_task or session.id,
            reason="Compute cell execution via local python",
            consequences="The code runs in a fresh local python "
                         "subprocess with the project directory as "
                         "its working directory."))
        self._audit(session.actor, "compute", "execute", False,
                    task_id=session.active_task or session.id,
                    reason="approval required")
        return {"allowed": False, "approval_required": True,
                "approval_request_id": request.id, "cell": None}

    def compute_execute(self, session: Session, code: str, *,
                        timeout: float | None = None,
                        approval_id: str = "") -> dict[str, Any]:
        """Run a compute cell — TERMINAL/execute policy first."""
        from forge.security.approvals import enforce_with_token
        from forge.security.policy import (PermissionEvaluation,
                                           PermissionRequest)

        if not isinstance(code, str) or not code.strip() \
                or len(code) > 6000 or "\x00" in code:
            raise InvalidRequest("Code must be 1-6000 characters, "
                                 "no nulls.")
        permission = PermissionRequest(
            agent="forge-compute", resource=Resource.TERMINAL,
            operation="execute", scope="python",
            task_id=session.active_task or session.id,
            reason="compute cell execution",
            details=(("args", ("-c", code.strip()[:6000])),))
        policy = self.policy if self.policy is not None else PermissionPolicy()
        evaluation = policy.evaluate(permission)
        self.audit.record_evaluation(permission, evaluation)
        if evaluation.decision == PolicyDecision.REQUIRE_APPROVAL \
                and approval_id:
            allowed, _reason = enforce_with_token(
                self.approval_store, approval_id, permission)
            if allowed:
                evaluation = PermissionEvaluation(
                    decision=PolicyDecision.ALLOW, reason=_reason,
                    risk=permission.risk, scope=permission.scope,
                    request_id=permission.request_id)
            else:
                return self._file_compute_approval(session)
        if evaluation.decision == PolicyDecision.REQUIRE_APPROVAL:
            return self._file_compute_approval(session)
        if evaluation.decision != PolicyDecision.ALLOW:
            self._audit(session.actor, "compute", "execute", False,
                        task_id=session.active_task or session.id,
                        reason=evaluation.reason)
            return {"allowed": False, "approval_required": False,
                    "approval_request_id": "", "cell": None,
                    "reason": evaluation.reason or "denied by policy"}
        engine = self._compute_engine(session)
        try:
            cell = engine.execute(code, timeout=timeout)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "compute", "execute", True,
                    task_id=session.active_task or session.id,
                    reason=f"status={cell['status']}, "
                           f"elapsed={cell['elapsed_ms']}ms")
        return {"allowed": True, "approval_required": False,
                "approval_request_id": "", "cell": cell}

    def compute_status(self, session: Session) -> dict[str, Any]:
        return self._compute_engine(session).backend_info()

    def compute_history(self, session: Session) -> dict[str, Any]:
        return {"cells": self._compute_engine(session).history()}

    def list_compute_approvals(self, session: Session
                               ) -> list[dict[str, Any]]:
        visible = []
        for request in self.approval_store.pending():
            if request.agent == "forge-compute" \
                    and request.task_id in (session.id, session.active_task):
                visible.append(request.to_dict())
        return visible

    def decide_compute_approval(self, session: Session, approval_id: str,
                                approved: bool) -> dict[str, Any]:
        request = self.approval_store.get_request(approval_id)
        if request is None or request.agent != "forge-compute" \
                or request.task_id not in (session.id, session.active_task):
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
        self._audit(session.actor, "compute",
                    "approve" if approved else "deny", True,
                    task_id=request.task_id,
                    reason=f"compute approval {approval_id}")
        return {"approval": decided.to_dict(), "token_id": token_id}


    # -- research / intelligence (A47) -----------------------------------------------------

    def _research_engine(self, session: Session):
        from forge.research.engine import ResearchEngine

        if not hasattr(self, "_research_engines"):
            self._research_engines: dict[str, Any] = {}
        project = self.get_project(session.project_id)
        engine = self._research_engines.get(project.id)
        if engine is None:
            engine = ResearchEngine(project.root)
            self._research_engines[project.id] = engine
        return engine

    def research_ask(self, session: Session,
                     question: str) -> dict[str, Any]:
        """Evidence-based research answer; never fabricated."""
        if not isinstance(question, str) or not question.strip() \
                or len(question) > 2000:
            raise InvalidRequest("Question must be 1-2000 characters.")
        engine = self._research_engine(session)
        result = engine.ask(question)
        self._audit(session.actor, "research", "ask", True,
                    task_id=session.active_task or session.id,
                    reason=f"evidence={len(result['evidence'])}, "
                           f"confidence={result['confidence']}")
        return result

    def research_report(self, session: Session) -> dict[str, Any]:
        engine = self._research_engine(session)
        result = engine.report()
        self._audit(session.actor, "research", "report", True,
                    task_id=session.active_task or session.id,
                    reason=f"sources={result['source_file_count']}")
        return result


    # -- AI council (A45) ---------------------------------------------------------------

    def _council_engine(self, session: Session):
        del session
        from forge.council.engine import AICouncilEngine

        if not hasattr(self, "_council_engines"):
            self._council_engines: dict[str, Any] = {}
            self._council_logs: dict[str, list[dict[str, Any]]] = {}
        if "default" not in self._council_engines:
            self._council_engines["default"] = AICouncilEngine()
        return self._council_engines["default"]

    def council_capabilities(self, session: Session) -> dict[str, Any]:
        del session
        engine = self._council_engine(None)  # type: ignore[arg-type]
        return {
            "members": [
                {"member": member.name, "model": member.model,
                 "stance": member.stance, "stance_label":
                 member.stance_label}
                for member in engine.members],
            "simulation": True,
            "advisory_only": True,
            "note": "Council verdicts are advisory input; they can "
                    "never authorize actions.",
        }

    def council_convene(self, session: Session,
                        question: str) -> dict[str, Any]:
        """Run the council over a question; honest, advisory verdict."""
        if not isinstance(question, str) or not question.strip() \
                or len(question) > 4000:
            raise InvalidRequest("Question must be 1-4000 characters.")
        engine = self._council_engine(session)
        result = engine.deliberate(question)
        self._council_logs.setdefault(session.id, []).append(result)
        self._council_logs[session.id] = self._council_logs[session.id][-12:]
        self._audit(session.actor, "council", "convene", True,
                    task_id=session.active_task or session.id,
                    reason=f"members={result['members']}, "
                           f"stance={result['stance']}, "
                           f"confidence={result['confidence']}")
        return result

    def council_history(self, session: Session) -> dict[str, Any]:
        return {"deliberations":
                list(self._council_logs.get(session.id, []))}



    # -- model fabric bridge (A46) ----------------------------------------------------------

    def _file_model_approval(self, session: Session, capability: str
                             ) -> dict[str, Any]:
        from forge.security.approvals import ApprovalRequest

        request = self.approval_store.submit(ApprovalRequest(
            agent="forge-model", resource=Resource.MODEL,
            operation="call", scopes=(capability,),
            task_id=session.active_task or session.id,
            reason=f"Model generation via fabric "
                   f"(capability={capability})",
            consequences="The prompt is routed through the model "
                         "fabric; the response is model output and is "
                         "treated as untrusted input."))
        self._audit(session.actor, "model", "generate", False,
                    task_id=session.active_task or session.id,
                    reason="approval required")
        return {"allowed": False, "approval_required": True,
                "approval_request_id": request.id,
                "prompt_hint": "",
                "response": None}

    def model_generate(self, session: Session, prompt: str, *,
                       capability: str = "coding",
                       approval_id: str = "") -> dict[str, Any]:
        """Gated fabric generation: MODEL/call policy first, then route."""
        from forge.models.bridge import FabricBridge
        from forge.security.approvals import enforce_with_token
        from forge.security.policy import (PermissionEvaluation,
                                           PermissionRequest)

        if not isinstance(prompt, str) or not prompt.strip() \
                or len(prompt) > 4000:
            raise InvalidRequest("Prompt must be 1-4000 characters.")
        if not isinstance(capability, str) or not capability.strip() \
                or len(capability) > 64:
            raise InvalidRequest("Invalid capability.")
        capability = capability.strip().lower()
        permission = PermissionRequest(
            agent="forge-model", resource=Resource.MODEL,
            operation="call", scope=capability,
            task_id=session.active_task or session.id,
            reason=f"fabric generation (capability={capability})",
            details=(("capability", capability),))
        policy = self.policy if self.policy is not None else PermissionPolicy()
        evaluation = policy.evaluate(permission)
        self.audit.record_evaluation(permission, evaluation)
        if evaluation.decision == PolicyDecision.REQUIRE_APPROVAL \
                and approval_id:
            allowed, _reason = enforce_with_token(
                self.approval_store, approval_id, permission)
            if allowed:
                evaluation = PermissionEvaluation(
                    decision=PolicyDecision.ALLOW, reason=_reason,
                    risk=permission.risk, scope=permission.scope,
                    request_id=permission.request_id)
            else:
                return self._file_model_approval(session, capability)
        if evaluation.decision == PolicyDecision.REQUIRE_APPROVAL:
            return self._file_model_approval(session, capability)
        if evaluation.decision != PolicyDecision.ALLOW:
            self._audit(session.actor, "model", "generate", False,
                        task_id=session.active_task or session.id,
                        reason=evaluation.reason)
            return {"allowed": False, "approval_required": False,
                    "approval_request_id": "",
                    "response": None,
                    "reason": evaluation.reason or "denied by policy"}
        bridge = FabricBridge(self.fabric)
        try:
            response = bridge.generate(prompt, capability=capability)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(session.actor, "model", "generate", True,
                    task_id=session.active_task or session.id,
                    reason=f"provider={response['provider']}, "
                           f"model={response['model']}, "
                           f"simulated={response['simulated']}")
        return {"allowed": True, "approval_required": False,
                "approval_request_id": "", "response": response}

    def list_model_approvals(self, session: Session
                             ) -> list[dict[str, Any]]:
        visible = []
        for request in self.approval_store.pending():
            if request.agent == "forge-model" \
                    and request.task_id in (session.id, session.active_task):
                visible.append(request.to_dict())
        return visible

    def decide_model_approval(self, session: Session, approval_id: str,
                              approved: bool) -> dict[str, Any]:
        request = self.approval_store.get_request(approval_id)
        if request is None or request.agent != "forge-model" \
                or request.task_id not in (session.id, session.active_task):
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
        self._audit(session.actor, "model",
                    "approve" if approved else "deny", True,
                    task_id=request.task_id,
                    reason=f"model approval {approval_id}")
        return {"approval": decided.to_dict(), "token_id": token_id}


    # -- AI-to-AI collaboration (A44) -------------------------------------------------

    def _collaboration(self, session: Session):
        from forge.collaboration.connectors import CollaborationSession

        if not hasattr(self, "_collaboration_sessions"):
            self._collaboration_sessions: dict[str, Any] = {}
        consult = self._collaboration_sessions.get(session.id)
        if consult is None:
            consult = CollaborationSession(session.id)
            self._collaboration_sessions[session.id] = consult
        return consult

    def collaboration_capabilities(self, session: Session) -> dict[str, Any]:
        del session
        from forge.collaboration.connectors import AVAILABLE_CONNECTORS

        return {
            "connectors": list(AVAILABLE_CONNECTORS),
            "untrusted_by_design": True,
            "simulated_only": True,
            "note": "External AI responses are marked untrusted and "
                    "can never authorize actions.",
        }

    def collaboration_consult(self, session: Session, question: str, *,
                              provider: str = "simulated-external",
                              approval_id: str = "") -> dict[str, Any]:
        """Ask an external AI — permission-gated, response untrusted."""
        from forge.collaboration.connectors import build_connector
        from forge.security.approvals import enforce_with_token
        from forge.security.policy import (PermissionEvaluation,
                                           PermissionRequest)

        if not isinstance(question, str) or not question.strip() \
                or len(question) > 4000:
            raise InvalidRequest("Question must be 1-4000 characters.")
        if len(provider) > 128:
            raise InvalidRequest("Invalid provider name.")
        try:
            connector = build_connector(provider)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from None
        permission = PermissionRequest(
            agent="forge-collaboration", resource=Resource.MODEL,
            operation="call", scope=provider,
            task_id=session.active_task or session.id,
            reason=f"external AI consultation via {provider}",
            details=(("provider", provider),))
        policy = self.policy if self.policy is not None else PermissionPolicy()
        evaluation = policy.evaluate(permission)
        self.audit.record_evaluation(permission, evaluation)
        consult = self._collaboration(session)
        if evaluation.decision == PolicyDecision.REQUIRE_APPROVAL \
                and approval_id:
            allowed, _reason = enforce_with_token(
                self.approval_store, approval_id, permission)
            if allowed:
                evaluation = PermissionEvaluation(
                    decision=PolicyDecision.ALLOW, reason=_reason,
                    risk=permission.risk, scope=permission.scope,
                    request_id=permission.request_id)
            else:
                # Spent/out-of-scope token: fail closed into a fresh
                # approval so the caller always gets a decidable id.
                from forge.security.approvals import ApprovalRequest
                refiled = self.approval_store.submit(ApprovalRequest(
                    agent="forge-collaboration", resource=Resource.MODEL,
                    operation="call", scopes=(provider,),
                    task_id=session.active_task or session.id,
                    reason=f"External AI consultation via {provider}",
                    consequences="The question is sent to the external "
                                 "connector; its answer is treated as "
                                 "untrusted input."))
                return {"allowed": False, "approval_required": True,
                        "approval_request_id": refiled.id,
                        "question": question[:400], "response": None,
                        "untrusted": True,
                        "reason": "approval token not valid or out of "
                                  "scope; a fresh approval was filed"}
        if evaluation.decision == PolicyDecision.REQUIRE_APPROVAL:
            from forge.security.approvals import ApprovalRequest
            filed = self.approval_store.submit(ApprovalRequest(
                agent="forge-collaboration", resource=Resource.MODEL,
                operation="call", scopes=(provider,),
                task_id=session.active_task or session.id,
                reason=f"External AI consultation via {provider}",
                consequences="The question is sent to the external "
                             "connector; its answer is treated as "
                             "untrusted input."))
            self._audit(session.actor, "ai-to-ai", "consult", False,
                        task_id=session.active_task or session.id,
                        reason="approval required")
            return {"allowed": False, "approval_required": True,
                    "approval_request_id": filed.id,
                    "question": question[:400], "response": None,
                    "untrusted": True}
        if evaluation.decision != PolicyDecision.ALLOW:
            self._audit(session.actor, "ai-to-ai", "consult", False,
                        task_id=session.active_task or session.id,
                        reason=evaluation.reason)
            return consult.record(question, {}, False,
                                  evaluation.reason or "denied by policy")
        response = connector.ask(question)
        self._audit(session.actor, "ai-to-ai", "consult", True,
                    task_id=session.active_task or session.id,
                    reason=f"provider={provider}, "
                           f"simulation={response['simulation']}")
        return consult.record(question, response, True)

    def collaboration_history(self, session: Session) -> dict[str, Any]:
        return {"history": self._collaboration(session).history()}

    def decide_collaboration_approval(self, session: Session,
                                      approval_id: str,
                                      approved: bool) -> dict[str, Any]:
        request = self.approval_store.get_request(approval_id)
        if request is None or request.agent != "forge-collaboration" \
                or request.task_id not in (session.id, session.active_task):
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
        self._audit(session.actor, "ai-to-ai",
                    "approve" if approved else "deny", True,
                    task_id=request.task_id,
                    reason=f"ai-to-ai approval {approval_id}")
        return {"approval": decided.to_dict(), "token_id": token_id}

    def list_collaboration_approvals(self, session: Session
                                     ) -> list[dict[str, Any]]:
        visible = []
        for request in self.approval_store.pending():
            if request.agent == "forge-collaboration" \
                    and request.task_id in (session.id, session.active_task):
                visible.append(request.to_dict())
        return visible


    # -- voice conversation (A42) ---------------------------------------------------

    def voice_conversation_start(self, session: Session) -> dict[str, Any]:
        """Open a bounded, interruptible voice conversation."""
        from forge.voice.conversation import new_conversation

        if not hasattr(self, "_voice_conversations"):
            self._voice_conversations: dict[str, Any] = {}
            self._voice_conversation_sessions: dict[str, list[str]] = {}
        owned = self._voice_conversation_sessions.setdefault(
            session.id, [])
        if len(owned) >= 4:
            raise Conflict("A session may hold at most 4 conversations")
        conversation = new_conversation(self._voice_stack().voice)
        self._voice_conversations[conversation.id] = conversation
        owned.append(conversation.id)
        self._audit(session.actor, "voice", "conversation_start", True,
                    task_id=session.active_task or session.id,
                    reason=f"conversation {conversation.id}")
        return {"conversation_id": conversation.id,
                "simulation": self._voice_stack().simulation}

    def _voice_conversation(self, session: Session,
                            conversation_id: str):
        conversations = getattr(self, "_voice_conversations", {})
        conversation = conversations.get(conversation_id)
        owned = getattr(self, "_voice_conversation_sessions", {}).get(
            session.id, [])
        if conversation is None or conversation_id not in owned:
            raise TaskNotFound(
                f"Unknown conversation: {conversation_id!r}")
        return conversation

    def voice_conversation_say(self, session: Session, conversation_id: str,
                               *, text: str = "", audio_b64: str = "",
                               approval_id: str = "",
                               confirm: bool = True) -> dict[str, Any]:
        """One conversational turn: context → intent → confirm → gate →
        act, with spoken results."""
        from forge.voice import AudioError

        if text and audio_b64:
            raise InvalidRequest("Provide either text or audio, not both.")
        if text:
            if not isinstance(text, str) or not text.strip() \
                    or len(text) > 2000:
                raise InvalidRequest("Voice text must be 1-2000 characters.")
            speech = text.strip()
        elif audio_b64:
            try:
                speech = self._voice_audio_chunk(audio_b64)
            except AudioError as exc:
                raise InvalidRequest(
                    f"Invalid audio: {exc.message}") from exc
        else:
            raise InvalidRequest("Provide text or audio for the turn.")
        conversation = self._voice_conversation(session, conversation_id)
        payload = conversation.say(
            speech, task_factory=self._voice_task_factory(session),
            approval_token_id=approval_id, confirm=confirm)
        self._audit(session.actor, "voice", "conversation_say", True,
                    task_id=session.active_task or session.id,
                    reason=(payload.get("intent")
                            or payload.get("status") or "turn"))
        return payload

    def voice_conversation_interrupt(self, session: Session,
                                     conversation_id: str
                                     ) -> dict[str, Any]:
        """Barge in: stop the active turn before any action runs."""
        conversation = self._voice_conversation(session, conversation_id)
        stopped = conversation.interrupt()
        self._audit(session.actor, "voice", "conversation_interrupt",
                    True, task_id=session.active_task or session.id,
                    reason=f"conversation {conversation_id}")
        return {"conversation_id": conversation_id,
                "interrupted": stopped}

    def voice_conversation_state(self, session: Session,
                                 conversation_id: str) -> dict[str, Any]:
        conversation = self._voice_conversation(session, conversation_id)
        return conversation.state()


    # -- vision (A39) ------------------------------------------------------------

    def _vision_permission(self, session: Session, *,
                           approval_id: str = "",
                           file_approval: bool = True) -> dict[str, Any]:
        """Evaluate one Resource.VISION / analyze through the A33 gate."""
        from forge.security.approvals import (ApprovalRequest,
                                              enforce_with_token)
        from forge.security.policy import PermissionRequest

        policy = self.policy if self.policy is not None else PermissionPolicy()
        permission = PermissionRequest(
            agent="forge-vision", resource=Resource.VISION,
            operation="analyze", scope="image",
            task_id=session.active_task or session.id,
            reason="vision analyze image")
        evaluation = policy.evaluate(permission)
        self.audit.record_evaluation(permission, evaluation)
        if evaluation.decision == PolicyDecision.REQUIRE_APPROVAL \
                and approval_id:
            allowed, _reason = enforce_with_token(
                self.approval_store, approval_id, permission)
            if allowed:
                return {"allowed": True, "decision": "ALLOW",
                        "approval_required": False,
                        "approval_request_id": approval_id}
        if evaluation.decision == PolicyDecision.REQUIRE_APPROVAL:
            request_id = ""
            if file_approval:
                filed = self.approval_store.submit(ApprovalRequest(
                    agent="forge-vision", resource=Resource.VISION,
                    operation="analyze", scopes=("image",),
                    task_id=session.active_task or session.id,
                    reason="Vision analyze an uploaded image",
                    consequences="The image bytes are sent to the "
                                 "configured vision provider."))
                request_id = filed.id
            return {"allowed": False, "decision": "REQUIRE_APPROVAL",
                    "approval_required": True,
                    "approval_request_id": request_id,
                    "reason": evaluation.reason}
        if evaluation.decision == PolicyDecision.ALLOW:
            return {"allowed": True, "decision": "ALLOW",
                    "approval_required": False,
                    "approval_request_id": ""}
        raise PolicyDeniedError(
            evaluation.reason or "Vision analyze denied by policy")

    def _vision_understand(self, session: Session,
                           image_b64: str) -> tuple[dict[str, Any], Any]:
        """Decode + permission-gate + analyze one image (shared path)."""
        from forge.vision.base import ImageFormatError
        from forge.vision.image_io import MAX_IMAGE_BYTES

        if not isinstance(image_b64, str) or not image_b64.strip():
            raise InvalidRequest("image_b64 must be a non-empty string.")
        if len(image_b64) > MAX_IMAGE_BYTES * 4 // 3 + 8:
            raise InvalidRequest("Image is too large.")
        import base64
        import binascii
        try:
            image = base64.b64decode(image_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InvalidRequest(f"Malformed base64 image: {exc}") from None
        permission = self._vision_permission(session)
        if not permission["allowed"]:
            return {"allowed": False, "simulation": True,
                    "approval_required": permission["approval_required"],
                    "approval_request_id":
                        permission.get("approval_request_id", ""),
                    "findings": [], "dangerous_instructions": [],
                    "summary": permission.get("reason", "")}, None
        try:
            result = self.vision_provider.analyze(image)
        except ImageFormatError as exc:
            raise InvalidRequest(str(exc)) from None
        except Exception as exc:
            from forge.vision.base import VisionUnavailable
            raise VisionUnavailable(str(exc)) from exc
        if result.error:
            raise InvalidRequest(result.error)
        return {"allowed": True, "approval_required": False,
                "approval_request_id": ""}, result

    def vision_analyze(self, session: Session, image_b64: str, *,
                       approval_id: str = "") -> dict[str, Any]:
        """Analyze one image (base64) through the permissioned provider."""
        import base64

        from forge.security.approvals import enforce_with_token
        from forge.security.policy import PermissionRequest

        if approval_id:
            permission = PermissionRequest(
                agent="forge-vision", resource=Resource.VISION,
                operation="analyze", scope="image",
                task_id=session.active_task or session.id,
                reason="vision analyze image")
            allowed, _reason = enforce_with_token(
                self.approval_store, approval_id, permission)
            if not allowed:
                return {"allowed": False, "simulation": True,
                        "approval_required": True,
                        "approval_request_id": approval_id,
                        "findings": [], "dangerous_instructions": [],
                        "summary": "approval token not valid"}
            head, result = self._vision_understand_ungated(session,
                                                           image_b64)
        else:
            head, result = self._vision_understand(session, image_b64)
        if not head["allowed"]:
            return head
        payload = result.to_dict()
        payload.update(head)
        self._audit(session.actor, "vision", "analyze", True,
                    task_id=session.active_task or session.id,
                    reason=f"format {result.format}, "
                           f"{len(base64.b64decode(image_b64))} bytes, "
                           f"simulation={result.simulation}")
        return payload

    def _vision_understand_ungated(self, session: Session,
                                   image_b64: str) -> tuple[dict[str, Any],
                                                             Any]:
        """Decode + analyze without a fresh policy evaluation (token
        redemption path only)."""
        import base64
        from forge.vision.base import ImageFormatError
        from forge.vision.image_io import MAX_IMAGE_BYTES

        if not isinstance(image_b64, str) or not image_b64.strip():
            raise InvalidRequest("image_b64 must be a non-empty string.")
        if len(image_b64) > MAX_IMAGE_BYTES * 4 // 3 + 8:
            raise InvalidRequest("Image is too large.")
        image = base64.b64decode(image_b64, validate=True)
        try:
            result = self.vision_provider.analyze(image)
        except ImageFormatError as exc:
            raise InvalidRequest(str(exc)) from None
        except Exception as exc:
            from forge.vision.base import VisionUnavailable
            raise VisionUnavailable(str(exc)) from exc
        if result.error:
            raise InvalidRequest(result.error)
        return {"allowed": True, "approval_required": False,
                "approval_request_id": ""}, result

    def vision_propose(self, session: Session,
                       image_b64: str) -> dict[str, Any]:
        """Analyze a screenshot and return policy-filtered proposals.

        Proposals are never executed here. Each action proposal is
        evaluated against Resource.VISION / execute: ALLOW → proposed,
        DENY → blocked, REQUIRE_APPROVAL → approval_required (filed,
        session-bound). Dangerous image text is always blocked. Any
        real execution later re-authorizes at the browser/desktop
        bridge under its own resource.
        """
        from forge.vision.pipeline import propose_actions

        head, result = self._vision_understand(session, image_b64)
        if not head["allowed"]:
            return {"allowed": False,
                    "approval_required": head["approval_required"],
                    "approval_request_id":
                        head.get("approval_request_id", ""),
                    "proposals": [],
                    "understanding": {"format": "",
                                      "summary": head.get("summary", "")}}
        proposals = propose_actions(result)
        from forge.security.approvals import ApprovalRequest
        from forge.security.policy import PermissionRequest

        policy = self.policy if self.policy is not None else PermissionPolicy()
        for proposal in proposals:
            if proposal["status"] == "blocked":
                continue
            if proposal["action"] == "observe":
                proposal["status"] = "proposed"
                continue
            permission = PermissionRequest(
                agent="forge-vision", resource=Resource.VISION,
                operation="execute", scope=proposal["target"],
                task_id=session.active_task or session.id,
                reason="screenshot-derived action proposal")
            evaluation = policy.evaluate(permission)
            self.audit.record_evaluation(permission, evaluation)
            if evaluation.decision == PolicyDecision.ALLOW:
                proposal["status"] = "proposed"
            elif evaluation.decision == PolicyDecision.DENY:
                proposal["status"] = "blocked"
                proposal["reason"] = (evaluation.reason
                                      or "blocked by policy")
            else:
                filed = self.approval_store.submit(ApprovalRequest(
                    agent="forge-vision", resource=Resource.VISION,
                    operation="execute", scopes=(proposal["target"],),
                    task_id=session.active_task or session.id,
                    reason="screenshot-derived action proposal",
                    consequences="Executing the proposed action still "
                                 "requires redeeming this approval "
                                 "through the target bridge."))
                proposal["status"] = "approval_required"
                proposal["approval_id"] = filed.id
                proposal["reason"] = (
                    "Operator approval required before this proposal "
                    "may be acted on.")
        self._audit(session.actor, "vision", "propose", True,
                    task_id=session.active_task or session.id,
                    reason=f"{len(proposals)} proposals, "
                           f"format {result.format}")
        return {"allowed": True, "approval_required": False,
                "approval_request_id": "", "proposals": proposals,
                "understanding": result.to_dict()}

    def list_vision_approvals(self, session: Session) -> list[dict[str, Any]]:
        visible = []
        for request in self.approval_store.pending():
            if request.agent == "forge-vision" and request.task_id in (
                    session.id, session.active_task):
                visible.append(request.to_dict())
        return visible

    def decide_vision_approval(self, session: Session, approval_id: str,
                               approved: bool) -> dict[str, Any]:
        request = self.approval_store.get_request(approval_id)
        if request is None or request.agent != "forge-vision" \
                or request.task_id not in (session.id, session.active_task):
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
        self._audit(session.actor, "vision",
                    "approve" if approved else "deny", True,
                    task_id=request.task_id,
                    reason=f"vision approval {approval_id}")
        return {"approval": decided.to_dict(), "token_id": token_id}


    # -- multi-agent orchestration (A38) ------------------------------------------

    def _orchestration_team(self, project: Project,
                            orchestration_id: str):
        """Build the default coordinated team for one orchestration.

        Every executor performs its real job over the project root:
        planner (capability planning), architect (deterministic
        structure proposal), researcher (bounded repository inventory),
        coder/debugger (model-driven change sets through the permissioned
        runtime), tester (bounded real test collection), reviewer
        (deterministic findings), security (bounded secret-pattern
        scan), performance (measured import timings), documentation
        (docs inventory), and git (real repository status).
        """
        import json as _json
        import importlib.util
        import os
        import subprocess
        import sys as _sys
        import time as _time

        from forge.agents.coder import CoderAgent
        from forge.agents.debugger import DebuggerAgent
        from forge.agents.execution import CallableAgentExecutor
        from forge.agents.planner import CapabilityAgentPlanner
        from forge.agents.registry import AgentRegistration, AgentRegistry
        from forge.intelligence.repository import RepositoryIntelligence
        from forge.runtime.defaults import create_default_runtime
        from forge.tools.git import GitTool

        root = str(project.root)
        intelligence = RepositoryIntelligence.build(root)
        permissions = PermissionManager(
            mode=OperationMode.ASSISTED, policy=self.policy,
            store=self.approval_store, agent="forge-orchestrator",
            audit=self.audit)
        runtime = create_default_runtime(permissions, root)
        approval_callback = self._orchestration_approval_callback(
            orchestration_id)
        registry = AgentRegistry()

        def bounded_files(suffixes, limit=200):
            found = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [name for name in dirnames
                               if name not in (".git", ".forge",
                                               "__pycache__", ".venv",
                                               "node_modules", ".arena")]
                for name in sorted(filenames):
                    if any(name.endswith(suffix) for suffix in suffixes):
                        found.append(os.path.relpath(
                            os.path.join(dirpath, name), root))
                        if len(found) >= limit:
                            return found
            return found

        def planner_worker(request):
            team_plan = CapabilityAgentPlanner(registry).plan(
                request.task.description)
            return _json.dumps({"capabilities": list(team_plan.capabilities),
                                "agents": list(team_plan.names)})

        def architect_worker(request):
            requirement = request.task.description.lower()
            proposals = []
            if "api" in requirement:
                proposals.append(
                    "one endpoint per concern, shared validation layer")
            if "export" in requirement or "csv" in requirement:
                proposals.append(
                    "separate writer module, used by the entry point")
            if "test" in requirement:
                proposals.append("tests live beside each new module")
            if not proposals:
                proposals.append("keep the change minimal and local")
            return _json.dumps(
                {"requirement": request.task.description[:400],
                 "proposal": proposals})

        def researcher_worker(request):
            del request
            python_files = bounded_files((".py",))
            test_files = [path for path in python_files if "test" in path]
            return _json.dumps({"python_files": len(python_files),
                                "test_files": len(test_files),
                                "sample": python_files[:20]})

        def coder_worker(request):
            agent = CoderAgent(
                runtime=runtime, root=root, fabric=self.fabric,
                approval_store=self.approval_store,
                approval_callback=approval_callback)
            response = agent.execute(request)
            if not response.success:
                raise RuntimeError(response.error or "coding step failed")
            return response.output

        def tester_worker(request):
            del request
            try:
                proc = subprocess.run(
                    [_sys.executable, "-m", "pytest", "--collect-only",
                     "-q"],
                    cwd=root, capture_output=True, text=True, timeout=30)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("test collection exceeded 30s") from exc
            if proc.returncode == 5:
                return "No tests found in the project."
            output = (proc.stdout or "") + (proc.stderr or "")
            return f"Test collection: exit={proc.returncode}\n{output[:3000]}"

        def debugger_worker(request):
            agent = DebuggerAgent(
                root=root, runtime=runtime, fabric=self.fabric,
                approval_store=self.approval_store,
                approval_callback=approval_callback)
            response = agent.execute(request)
            if not response.success:
                raise RuntimeError(response.error or "debugging step failed")
            return response.output

        def reviewer_worker(request):
            python_files = bounded_files((".py",))
            findings = [
                f"repository exposes {len(python_files)} python files"]
            if not any("test" in name for name in python_files):
                findings.append("no test files found")
            requirement = request.task.description.lower()
            for capability in ("coding", "testing", "security"):
                if capability in requirement:
                    findings.append(
                        f"requirement mentions {capability}")
            return _json.dumps({"findings": findings})

        _SECRET_RE = re.compile(
            r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*"
            r"['\"][^'\"]{8,}['\"]")

        def security_worker(request):
            del request
            findings = []
            for path in bounded_files(
                    (".py", ".json", ".toml", ".yaml", ".yml", ".env"),
                    limit=150):
                try:
                    content = Path(root, path).read_text(
                        encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for match in _SECRET_RE.finditer(content):
                    findings.append({
                        "file": path,
                        "line": content[:match.start()].count("\n") + 1,
                        "pattern": match.group(1)})
            return _json.dumps({"secrets_found": len(findings),
                                "findings": findings[:50]})

        def performance_worker(request):
            del request
            measurements = []
            for path in bounded_files((".py",), limit=30):
                if "test" in path:
                    continue
                try:
                    spec = importlib.util.spec_from_file_location(
                        f"forge_perf_probe_{len(measurements)}",
                        Path(root, path))
                    module = importlib.util.module_from_spec(spec)
                    started = _time.perf_counter()
                    spec.loader.exec_module(module)
                    measurements.append({
                        "module": path,
                        "import_ms": round(
                            (_time.perf_counter() - started) * 1000, 3)})
                except Exception as exc:
                    measurements.append({
                        "module": path,
                        "import_error": str(exc)[:200]})
            return _json.dumps({"measurements": measurements})

        def documentation_worker(request):
            del request
            markdown = bounded_files((".md", ".rst"), limit=50)
            return _json.dumps({"docs_files": markdown,
                                "readme": "README.md" in markdown})

        def git_worker(request):
            del request
            git = GitTool(root)
            return _json.dumps({"status": git.status(),
                                "last_commit": git.run(
                                    "log", "-1", "--oneline").stdout.strip()})

        registry.register(AgentRegistration(
            "planner", "planning",
            CallableAgentExecutor("planner", planner_worker),
            ("planning",)))
        registry.register(AgentRegistration(
            "architect", "architecture",
            CallableAgentExecutor("architect", architect_worker),
            ("architecture",)))
        registry.register(AgentRegistration(
            "researcher", "research",
            CallableAgentExecutor("researcher", researcher_worker),
            ("research",)))
        registry.register(AgentRegistration(
            "coder", "coding",
            CallableAgentExecutor("coder", coder_worker), ("coding",)))
        registry.register(AgentRegistration(
            "tester", "testing",
            CallableAgentExecutor("tester", tester_worker), ("testing",)))
        registry.register(AgentRegistration(
            "debugger", "debugging",
            CallableAgentExecutor("debugger", debugger_worker),
            ("debugging",)))
        registry.register(AgentRegistration(
            "reviewer", "reviewing",
            CallableAgentExecutor("reviewer", reviewer_worker),
            ("review",)))
        registry.register(AgentRegistration(
            "security", "security",
            CallableAgentExecutor("security", security_worker),
            ("security",)))
        registry.register(AgentRegistration(
            "performance", "performance",
            CallableAgentExecutor("performance", performance_worker),
            ("performance",)))
        registry.register(AgentRegistration(
            "documentation", "documentation",
            CallableAgentExecutor("documentation", documentation_worker),
            ("documentation",)))
        registry.register(AgentRegistration(
            "git", "git", CallableAgentExecutor("git", git_worker),
            ("git",)))
        return registry

    def submit_orchestration(self, session: Session, requirement: str, *,
                             chain: bool = False):
        """Queue one multi-agent orchestration for the session's project."""
        if not isinstance(requirement, str) or not requirement.strip():
            raise InvalidRequest("Requirement must be a non-empty string.")
        requirement = requirement.strip()
        if len(requirement) > MAX_REQUIREMENT_CHARS:
            raise InvalidRequest("Requirement is too long.")
        project = self.get_project(session.project_id)
        record = self.orchestrations.create(
            session_id=session.id, project_id=project.id,
            requirement=requirement, actor=session.actor)
        self._emit(record.id, record.project_id, "orchestration.created",
                   {"chain": bool(chain), "actor": session.actor,
                    "requirement_chars": len(requirement)})
        self._audit(session.actor, "orchestration", "submit", True,
                    task_id=record.id,
                    reason=f"project {project.id}, chain={bool(chain)}")
        if self._executor is not None:
            self._executor.submit(
                self._execute_orchestration, record.id, bool(chain))
        return record

    def get_orchestration(self, session: Session, orchestration_id: str,
                          *, include_report: bool = True):
        validate_id(orchestration_id, kind="orchestration id")
        record = self.orchestrations.get(orchestration_id)
        # Session-scoped: cross-session ids map to NOT_FOUND.
        if record is None or record.session_id != session.id:
            raise TaskNotFound(
                f"Unknown orchestration: {orchestration_id!r}")
        return record

    def list_orchestrations(self, session: Session):
        return self.orchestrations.list_for_session(session.id)

    def cancel_orchestration(self, session: Session, orchestration_id: str):
        from forge.control.orchestrations import TERMINAL as ORCH_TERMINAL

        record = self.get_orchestration(session, orchestration_id)
        if record.status in ORCH_TERMINAL:
            raise InvalidRequest("Orchestration already finished.")
        control = self._orch_control(record.id)
        if control is None:
            raise InvalidRequest("Orchestration is not running yet.")
        control.request_cancel()
        self._audit(session.actor, "orchestration", "cancel", True,
                    task_id=record.id)
        return self.orchestrations.get(record.id) or record

    def list_orchestration_approvals(self, session: Session,
                                     orchestration_id: str):
        record = self.get_orchestration(session, orchestration_id)
        visible = []
        for request in self.approval_store.pending():
            # Scoped by the orchestration's own task id; the requesting
            # agent (coder, debugger, forge-orchestrator) is reported
            # but never decides.
            if request.task_id == record.id:
                visible.append(request.to_dict())
        return visible

    def decide_orchestration_approval(self, session: Session,
                                      orchestration_id: str,
                                      approval_id: str,
                                      approved: bool) -> dict[str, Any]:
        record = self.get_orchestration(session, orchestration_id)
        request = self.approval_store.get_request(approval_id)
        if request is None or request.task_id != record.id:
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
        with self._orch_lock:
            event = self._orch_events.get(approval_id)
        if event is not None:
            event.set()
        self._audit(session.actor, "orchestration",
                    "approve" if approved else "deny", True,
                    task_id=record.id,
                    reason=f"orchestration approval {approval_id} "
                    f"{'approved' if approved else 'denied'}")
        return {"approval": decided.to_dict(), "token_id": token_id}

    # -- orchestration internals --------------------------------------------------

    def _orch_control(self, orchestration_id: str):
        with self._orch_lock:
            return self._orch_controls.get(orchestration_id)

    def _wait_approval_token(self, request, orchestration_id: str,
                             control: SupervisorControl, timeout: float):
        """Block until the operator decides one approval; mint a token.

        Returns ``""`` when denied/expired/timed out (fail closed) and
        raises ``TaskCancelled`` when the orchestration is cancelled.
        """
        from forge.core.run_control import TaskCancelled
        from forge.security.approvals import ApprovalStatus

        event = threading.Event()
        with self._orch_lock:
            self._orch_events[request.id] = event
        deadline = time.time() + max(1.0, timeout)
        try:
            while True:
                if control.cancel_requested:
                    raise TaskCancelled(
                        "cancelled while awaiting approval")
                if event.wait(timeout=0.1) or time.time() >= deadline:
                    break
                if request.status != ApprovalStatus.PENDING:
                    break
            if request.status != ApprovalStatus.APPROVED:
                return ""
            # One redeem per covered path: a multi-file change set must
            # not fail closed on its second write.
            uses = len(request.files) if request.files \
                else len(request.scopes)
            token = self.approval_store.issue(
                request.id, request.decided_by,
                ttl_seconds=self.config.approval_token_ttl,
                max_uses=max(1, uses))
            return token.id
        finally:
            with self._orch_lock:
                self._orch_events.pop(request.id, None)

    def _orchestration_approval_callback(
            self, orchestration_id: str) -> Callable[[Any], str]:
        """Operator hook consulted for agent dispatches and change sets.

        Dispatch queries file ``Resource.AGENT / execute`` requests;
        coder/debugger change-set queries file translated filesystem
        requests through the A33 service. Every wait is bounded by the
        approval timeout and honours cancellation.
        """
        from forge.control.orchestrations import TERMINAL as ORCH_TERMINAL
        from forge.core.orchestrator import DispatchQuery
        from forge.core.run_control import TaskCancelled
        from forge.security.approvals import ApprovalRequest

        def callback(query: Any) -> str:
            record = self.orchestrations.get(orchestration_id)
            if record is None or record.status in ORCH_TERMINAL:
                return ""
            control = self._orch_control(orchestration_id)
            if control is not None and control.cancel_requested:
                raise TaskCancelled("cancelled before approval")
            if isinstance(query, DispatchQuery):
                request = ApprovalRequest(
                    agent="forge-orchestrator", resource=Resource.AGENT,
                    operation="execute", scopes=(query.agent,),
                    task_id=orchestration_id,
                    reason=query.reason[:1000],
                    consequences=(
                        f"Agent {query.agent} runs one step of the "
                        f"orchestration."),
                    expires_at=time.time() + self.config.approval_timeout)
                self.approval_store.submit(request)
                self.orchestrations.mutate(
                    orchestration_id,
                    status=OrchestrationStatus.WAITING_APPROVAL,
                    stage=f"approval:{query.agent}")
                self._emit(orchestration_id, record.project_id,
                           "approval.required",
                           {"approval_id": request.id,
                            "agent": "forge-orchestrator",
                            "task_id": orchestration_id,
                            "tool": "agent.dispatch",
                            "operation": "agent:execute",
                            "scope": [query.agent],
                            "reason": request.reason,
                            "label": query.label})
                token = self._wait_approval_token(
                    request, orchestration_id, control,
                    self.config.approval_timeout)
                if token:
                    self.orchestrations.mutate(
                        orchestration_id,
                        status=OrchestrationStatus.RUNNING, stage="running")
                return token
            # Change-set query (coder/debugger writes): translate through
            # the A33 approval service and wait on the filed requests.
            adapter = type("ApprovalRun", (), {
                "id": orchestration_id,
                "project_id": record.project_id})()
            try:
                filed = self.approvals.file_query(
                    query, adapter, model="", provider="")
            except ApprovalError:
                return ""
            tokens = [
                self._wait_approval_token(request, orchestration_id,
                                         control,
                                         self.config.approval_timeout)
                for request in filed]
            return tokens[0] if len(tokens) == 1 else ""
        return callback

    def _orchestration_sink(self, orchestration_id: str) -> Callable[
            [str, dict[str, Any]], None]:
        record = self.orchestrations.get(orchestration_id)
        project_id = record.project_id if record is not None else ""

        def sink(name: str, details: dict[str, Any]) -> None:
            self._emit(orchestration_id, project_id,
                       f"orchestration.{name}", details)
        return sink

    def _execute_orchestration(self, orchestration_id: str,
                               chain: bool = False) -> None:
        from forge.control.orchestrations import TERMINAL as ORCH_TERMINAL
        from forge.core.orchestrator import (MultiAgentOrchestrator,
                                             ReportStatus)

        record = self.orchestrations.get(orchestration_id)
        if record is None:
            return
        project_id = record.project_id
        try:
            if record.status != OrchestrationStatus.QUEUED:
                return
            updated = self.orchestrations.compare_and_set(
                record.id, record.version,
                status=OrchestrationStatus.RUNNING, stage="planning",
                started_at=time.time())
            if updated is None:
                return
            record = updated
            project = self.get_project(project_id)
            self._emit(record.id, project_id, "orchestration.started",
                       {"chain": bool(chain)})
            control = SupervisorControl()
            with self._orch_lock:
                self._orch_controls[record.id] = control
            registry = self._orchestration_team(project, record.id)
            orchestrator = MultiAgentOrchestrator(
                registry,
                max_workers=self.config.orchestration_max_workers,
                max_attempts=self.config.orchestration_max_attempts,
                step_timeout=self.config.orchestration_step_timeout,
                policy=self.policy,
                approval_store=self.approval_store,
                approval_callback=self._orchestration_approval_callback(
                    record.id),
                on_event=self._orchestration_sink(record.id),
                control=control,
                task_id=record.id)
            plan = orchestrator.build_plan(record.requirement, chain=chain)
            self.orchestrations.mutate(
                record.id, stage="running",
                plan_json=json.dumps(plan.to_dict(), default=str))
            report = orchestrator.execute(plan)
            status_map = {
                ReportStatus.SUCCEEDED: OrchestrationStatus.SUCCEEDED,
                ReportStatus.FAILED: OrchestrationStatus.FAILED,
                ReportStatus.CANCELLED: OrchestrationStatus.CANCELLED,
                ReportStatus.PLAN_REJECTED: OrchestrationStatus.FAILED,
            }
            status = status_map[report.status]
            error = "" if status == OrchestrationStatus.SUCCEEDED \
                else report.summary
            self.orchestrations.mutate(
                record.id, status=status, stage="finished",
                report_json=json.dumps(report.to_dict(), default=str),
                finished_at=time.time(), error=error)
            self._emit(record.id, project_id, "orchestration.finished",
                       {"status": status.value,
                        "accepted": report.accepted,
                        "summary": report.summary})
        except Exception as exc:  # pragma: no cover - defensive
            try:
                self.orchestrations.mutate(
                    orchestration_id, status=OrchestrationStatus.FAILED,
                    stage="failed", error=f"Worker error: {exc}",
                    finished_at=time.time())
            except Exception:
                pass
        finally:
            with self._orch_lock:
                self._orch_controls.pop(orchestration_id, None)


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
            error_text = str(outcome.get("error", "Run failed."))[:2000]
            updates.update(status=RunStatus.FAILED, stage="failed",
                           error=error_text)
        if updates.get("status") == RunStatus.FAILED:
            try:  # learning must never break the pipeline
                self._failure_ledger().record(
                    "task", updates.get("error") or "Run failed.",
                    task_id=run_id,
                    actor=getattr(run, "actor", "") or "")
            except Exception:
                pass
        self.runs.mutate(run_id, **updates)
        finished = self.runs.get(run_id)
        if finished is None:
            return
        try:
            if finished.status == RunStatus.SUCCEEDED:
                self._metrics().incr("runs.succeeded")
            elif finished.status == RunStatus.FAILED:
                self._metrics().incr("runs.failed")
            if run.started_at and finished.finished_at:
                self._metrics().observe(
                    "run.duration_ms",
                    finished.finished_at - run.started_at)
        except Exception:
            pass
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
        self._record_run_memory(finished)

    def _record_run_memory(self, run: Run) -> None:
        """Append a bounded run summary to durable project memory (A37)."""
        if not self.config.memory_record_runs:
            return
        try:
            store = self._project_memory(run.project_id)
            store.save(
                f"runs/{run.id}",
                json.dumps({
                    "status": run.status.value,
                    "requirement": run.requirement[:500],
                    "model": run.model, "provider": run.provider,
                    "attempts": run.attempts, "rollback": run.rollback,
                    "finished_at": run.finished_at,
                }, default=str))
            # Bounded retention: keep the most recent 50 run summaries.
            keys = [key for key in store.list()
                    if key.startswith("runs/")]
            if len(keys) > 50:
                def mtime(key: str) -> float:
                    path = store._safe_path(key)
                    try:
                        return path.stat().st_mtime
                    except OSError:
                        return 0.0
                for key in sorted(keys, key=mtime)[:len(keys) - 50]:
                    store.delete(key)
        except Exception:
            pass  # memory recording never breaks a finished run

    def _finish_run(self, run_id: str, status: RunStatus,
                    *, error: str = "") -> None:
        run = self.runs.get(run_id)
        if run is None or run.status in TERMINAL_STATUSES:
            return
        finished_at = time.time()
        self.runs.mutate(run.id, status=status,
                         stage=status.value.lower(),
                         error=error[:2000], finished_at=finished_at)
        try:
            if status == RunStatus.SUCCEEDED:
                self._metrics().incr("runs.succeeded")
            elif status == RunStatus.FAILED:
                self._metrics().incr("runs.failed")
            if run.started_at:
                self._metrics().observe("run.duration_ms",
                                        finished_at - run.started_at)
        except Exception:
            pass
        if status == RunStatus.FAILED and error:
            try:  # learning must never break the pipeline
                self._failure_ledger().record(
                    "task", error, task_id=run.id,
                    actor=getattr(run, "actor", "") or "")
            except Exception:
                pass
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

