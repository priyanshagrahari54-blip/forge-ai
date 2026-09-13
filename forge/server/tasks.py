"""Task management: the durable lifecycle authority (A81).

:class:`TaskManager` owns the ``tasks`` table. Every status change goes
through :meth:`transition`, which enforces the closed transition table
from :mod:`forge.server.models`, bumps the optimistic-concurrency
``version``, and stamps the right timestamps. Field updates go through
:meth:`update` with a strict whitelist — arbitrary columns cannot be
written, so a buggy or hostile caller can never reshape task state.

Startup recovery (:meth:`recover_interrupted`) makes crashes honest:
tasks a dead process left in ``started``/``running``/
``waiting_for_approval`` are re-queued while retries remain, and failed
with a clear error once they are exhausted. Nothing resumes silently in
a half-known state.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Sequence
from uuid import uuid4

from forge.server.errors import (
    Conflict,
    InvalidRequest,
    InvalidTransition,
    TaskNotFound,
    VersionConflict,
)
from forge.server.models import (
    ACTIVE_STATUSES,
    INTERRUPTED_STATUSES,
    MAX_REQUIREMENT_CHARS,
    TERMINAL_STATUSES,
    ServerTask,
    TaskStatus,
    can_transition,
    task_from_row,
    validate_id,
)
from forge.server.storage import Database

#: Columns :meth:`TaskManager.update` may touch. Closed on purpose.
UPDATABLE_FIELDS = frozenset({
    "stage", "progress", "checkpoint_id", "error", "result_json",
    "retry_count", "cancel_requested", "pause_requested", "available_at",
    "priority"})

_TASK_COLUMNS = (
    "task_id, project_id, requirement, status, stage, progress, priority, "
    "mode, actor, version, retry_count, max_retries, checkpoint_id, "
    "cancel_requested, pause_requested, available_at, result_json, error, "
    "created_at, queued_at, started_at, updated_at, finished_at")


class TaskManager:
    """Persistent task lifecycle management over SQLite."""

    def __init__(self, db: Database, *,
                 default_max_retries: int = 2) -> None:
        self._db = db
        self.default_max_retries = max(0, int(default_max_retries))

    # -- creation ------------------------------------------------------------

    def create(self, project_id: str, requirement: str, *,
               priority: int = 0, mode: str = "assisted", actor: str = "",
               max_retries: Optional[int] = None,
               task_id: Optional[str] = None) -> ServerTask:
        """Persist a new task in ``created`` state."""
        try:
            project_id = validate_id(project_id, kind="project_id")
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        if not isinstance(requirement, str) or not requirement.strip():
            raise InvalidRequest("requirement must be a non-empty string.")
        if len(requirement) > MAX_REQUIREMENT_CHARS:
            raise InvalidRequest(
                "requirement exceeds %d characters." % MAX_REQUIREMENT_CHARS)
        if "\x00" in requirement:
            raise InvalidRequest("requirement contains a null byte.")
        identifier = task_id or ("task-" + uuid4().hex[:16])
        try:
            identifier = validate_id(identifier, kind="task_id")
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        retries = (self.default_max_retries if max_retries is None
                   else int(max_retries))
        if retries < 0 or retries > 10:
            raise InvalidRequest("max_retries must be between 0 and 10.")
        moment = time.time()
        task = ServerTask(
            task_id=identifier, project_id=project_id,
            requirement=requirement.strip(),
            status=TaskStatus.CREATED, priority=int(priority), mode=mode,
            actor=actor, max_retries=retries, created_at=moment,
            updated_at=moment)
        self._db.execute(
            "INSERT INTO tasks (%s) VALUES (%s)"
            % (_TASK_COLUMNS, ", ".join("?" * 23)),
            self._to_row(task))
        return task

    # -- reads ---------------------------------------------------------------

    def get(self, task_id: str) -> Optional[ServerTask]:
        row = self._db.query_one(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        return task_from_row(row) if row is not None else None

    def get_or_raise(self, task_id: str) -> ServerTask:
        task = self.get(task_id)
        if task is None:
            raise TaskNotFound("Unknown task: %r" % task_id,
                               task_id=task_id)
        return task

    def list(self, *, project_id: str = "", status: str = "",
             limit: int = 50) -> List[ServerTask]:
        limit = max(1, min(500, int(limit)))
        sql = "SELECT * FROM tasks"
        clauses: List[str] = []
        params: List[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if status:
            clauses.append("status = ?")
            params.append(TaskStatus(status).value)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [task_from_row(row) for row in self._db.query(sql, params)]

    def active_tasks(self, project_id: str = "") -> List[ServerTask]:
        active_values = [status.value for status in ACTIVE_STATUSES]
        sql = ("SELECT * FROM tasks WHERE status IN (%s)"
               % ", ".join("?" * len(active_values)))
        params: List[Any] = list(active_values)
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        sql += " ORDER BY created_at ASC"
        return [task_from_row(row) for row in self._db.query(sql, params)]

    def updated_since(self, timestamp: float, *,
                      project_id: str = "",
                      limit: int = 200) -> List[ServerTask]:
        limit = max(1, min(500, int(limit)))
        sql = "SELECT * FROM tasks WHERE updated_at >= ?"
        params: List[Any] = [float(timestamp)]
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return [task_from_row(row) for row in self._db.query(sql, params)]

    def status_counts(self, project_id: str = "") -> Dict[str, int]:
        sql = "SELECT status, COUNT(*) AS n FROM tasks"
        params: List[Any] = []
        if project_id:
            sql += " WHERE project_id = ?"
            params.append(project_id)
        sql += " GROUP BY status"
        counts = {status.value: 0 for status in TaskStatus}
        for row in self._db.query(sql, params):
            counts[row["status"]] = int(row["n"])
        return counts

    # -- writes ----------------------------------------------------------------

    def transition(self, task_id: str, new_status: "TaskStatus | str", *,
                   expected: "Optional[Sequence[TaskStatus]]" = None,
                   expected_version: Optional[int] = None,
                   **fields: Any) -> ServerTask:
        """Move a task to ``new_status`` through the closed state machine.

        ``expected`` narrows the legal *source* statuses further (workers
        pass e.g. ``(QUEUED,)`` so a concurrently cancelled task is never
        started). ``expected_version`` adds optimistic concurrency for
        API callers. Timestamps, version bumps, and the transition table
        are enforced here — nowhere else.
        """
        new_status = TaskStatus(new_status)
        task = self.get_or_raise(task_id)
        if expected is not None and task.status not in tuple(expected):
            raise InvalidTransition(
                "Task is %s; expected one of: %s."
                % (task.status.value,
                   ", ".join(sorted(s.value for s in expected))),
                task_id=task_id, status=task.status.value)
        if not can_transition(task.status, new_status):
            raise InvalidTransition(
                "Transition %s -> %s is not allowed."
                % (task.status.value, new_status.value),
                task_id=task_id, status=task.status.value,
                target=new_status.value)
        if (expected_version is not None
                and int(expected_version) != task.version):
            raise VersionConflict(
                "Task version changed: expected %d, found %d."
                % (int(expected_version), task.version),
                task_id=task_id, expected_version=int(expected_version),
                version=task.version)
        updates: Dict[str, Any] = dict(fields)
        moment = time.time()
        updates["status"] = new_status.value
        updates["updated_at"] = moment
        if new_status == TaskStatus.QUEUED and task.queued_at is None:
            updates["queued_at"] = moment
        if new_status == TaskStatus.STARTED and task.started_at is None:
            updates["started_at"] = moment
        if new_status in TERMINAL_STATUSES:
            updates["finished_at"] = moment
            updates["cancel_requested"] = False
            updates["pause_requested"] = False
            if new_status == TaskStatus.COMPLETED:
                updates["stage"] = "completed"
                updates["progress"] = 1.0
        return self._write(task, updates)

    def update(self, task_id: str, **fields: Any) -> ServerTask:
        """Update whitelisted fields without changing status."""
        unknown = set(fields) - UPDATABLE_FIELDS
        if unknown:
            raise InvalidRequest(
                "Cannot update field(s): %s" % ", ".join(sorted(unknown)))
        task = self.get_or_raise(task_id)
        return self._write(task, dict(fields))

    def _write(self, task: ServerTask, updates: Dict[str, Any]) -> ServerTask:
        allowed = UPDATABLE_FIELDS | {
            "status", "queued_at", "started_at", "finished_at", "updated_at"}
        unknown = set(updates) - allowed
        if unknown:
            raise InvalidRequest(
                "Cannot write field(s): %s" % ", ".join(sorted(unknown)))
        assignments = ["version = version + 1"]
        params: List[Any] = []
        for key in sorted(updates):
            value = updates[key]
            if isinstance(value, TaskStatus):
                value = value.value
            if isinstance(value, bool):
                value = int(value)
            if value is not None and not isinstance(value, (int, float, str)):
                value = json.dumps(value, default=str)
            assignments.append("%s = ?" % key)
            params.append(value)
        params.extend([task.task_id, task.version])
        cursor = self._db.execute(
            "UPDATE tasks SET %s WHERE task_id = ? AND version = ?"
            % ", ".join(assignments), params)
        if cursor.rowcount != 1:
            # Lost a race with another writer: fail closed, never clobber.
            raise VersionConflict(
                "Concurrent task modification detected.",
                task_id=task.task_id, version=task.version)
        return self.get_or_raise(task.task_id)

    # -- recovery --------------------------------------------------------------

    def recover_interrupted(self) -> List[Dict[str, Any]]:
        """Fix tasks a previous process left mid-flight.

        Returns one record per recovered task:
        ``{"task_id", "action": "requeued"|"failed", "retry_count"}``.
        Re-queued tasks keep their identity and history; the caller is
        responsible for putting them back on the queue.
        """
        interrupted = [status.value for status in INTERRUPTED_STATUSES]
        rows = self._db.query(
            "SELECT * FROM tasks WHERE status IN (%s)"
            % ", ".join("?" * len(interrupted)), interrupted)
        recovered: List[Dict[str, Any]] = []
        for row in rows:
            task = task_from_row(row)
            if task.retry_count < task.max_retries:
                updated = self.transition(
                    task.task_id, TaskStatus.QUEUED,
                    retry_count=task.retry_count + 1,
                    stage="requeued", error="")
                recovered.append({
                    "task_id": task.task_id, "action": "requeued",
                    "retry_count": updated.retry_count})
            else:
                self.transition(
                    task.task_id, TaskStatus.FAILED,
                    error="Interrupted by Forge Server restart; retry "
                          "budget exhausted (%d/%d)."
                          % (task.retry_count, task.max_retries))
                recovered.append({
                    "task_id": task.task_id, "action": "failed",
                    "retry_count": task.retry_count})
        return recovered

    def expire_paused_queue_items(self) -> int:
        """Count paused tasks (informational; pause survives restarts)."""
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM tasks WHERE status = ?",
            (TaskStatus.PAUSED.value,))
        return int(row["n"]) if row else 0

    # -- internals ---------------------------------------------------------------

    @staticmethod
    def _to_row(task: ServerTask) -> tuple:
        return (
            task.task_id, task.project_id, task.requirement,
            task.status.value, task.stage, float(task.progress),
            int(task.priority), task.mode, task.actor, int(task.version),
            int(task.retry_count), int(task.max_retries),
            task.checkpoint_id, int(task.cancel_requested),
            int(task.pause_requested), float(task.available_at),
            task.result_json, task.error, task.created_at, task.queued_at,
            task.started_at, task.updated_at, task.finished_at)
