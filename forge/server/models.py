"""Core Forge Server domain models (A81).

The task lifecycle is a closed state machine. Every status named in the
Forge Server contract exists exactly once here, and
:data:`TRANSITIONS` is the single authority for which edges are legal —
:meth:`TaskManager.transition <forge.server.tasks.TaskManager.transition>`
refuses anything else, so no code path (API, worker, recovery) can
smuggle a task into an impossible state.

Lifecycle::

    created → queued → started → running → completed
                  │         │        │  ↖         ↘ rolled_back
                  │         │        ├→ paused ⇄ running
                  │         │        ├→ waiting_for_approval ⇄ running
                  │         │        └→ failed → queued (retry, bounded)
                  └─────────┴──────────→ cancelled
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

#: Hard cap on one requirement; larger payloads are rejected at the API.
MAX_REQUIREMENT_CHARS = 8000

#: Task/project ids are audit identifiers, not free text.
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class TaskStatus(str, Enum):
    """The ten Forge Server task states."""

    CREATED = "created"
    QUEUED = "queued"
    STARTED = "started"
    PAUSED = "paused"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ROLLED_BACK = "rolled_back"


#: Closed transition table. Anything absent here is refused.
TRANSITIONS: "Dict[TaskStatus, frozenset]" = {
    TaskStatus.CREATED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    TaskStatus.QUEUED: frozenset({
        TaskStatus.STARTED, TaskStatus.PAUSED, TaskStatus.CANCELLED}),
    TaskStatus.STARTED: frozenset({
        TaskStatus.RUNNING, TaskStatus.QUEUED, TaskStatus.FAILED,
        TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset({
        TaskStatus.PAUSED, TaskStatus.WAITING_FOR_APPROVAL,
        TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED,
        TaskStatus.QUEUED}),
    TaskStatus.PAUSED: frozenset({
        TaskStatus.RUNNING, TaskStatus.QUEUED, TaskStatus.CANCELLED,
        TaskStatus.FAILED}),
    TaskStatus.WAITING_FOR_APPROVAL: frozenset({
        TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        # Restart recovery re-queues tasks whose approval died with the
        # previous process; the run asks again if it still needs one.
        TaskStatus.QUEUED}),
    TaskStatus.COMPLETED: frozenset({TaskStatus.ROLLED_BACK}),
    TaskStatus.FAILED: frozenset({TaskStatus.QUEUED, TaskStatus.ROLLED_BACK}),
    TaskStatus.CANCELLED: frozenset({
        TaskStatus.QUEUED, TaskStatus.ROLLED_BACK}),
    TaskStatus.ROLLED_BACK: frozenset(),
}

#: States a task can never leave.
TERMINAL_STATUSES = frozenset({
    TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED,
    TaskStatus.ROLLED_BACK})

#: States where the server is actively responsible for the task.
ACTIVE_STATUSES = frozenset({
    TaskStatus.QUEUED, TaskStatus.STARTED, TaskStatus.RUNNING,
    TaskStatus.PAUSED, TaskStatus.WAITING_FOR_APPROVAL})

#: States left dangling by a crash; startup recovery re-queues or fails them.
INTERRUPTED_STATUSES = frozenset({
    TaskStatus.STARTED, TaskStatus.RUNNING, TaskStatus.WAITING_FOR_APPROVAL})


def can_transition(current: TaskStatus, new: TaskStatus) -> bool:
    return new in TRANSITIONS.get(TaskStatus(current), frozenset())


def validate_id(value: Any, *, kind: str = "id") -> str:
    """Fail closed on malformed identifiers."""
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ValueError(
            "Malformed %s: want 1-128 chars starting alphanumeric, "
            "then letters/digits/_.-" % kind)
    return value


def now() -> float:
    return time.time()


@dataclass
class ServerTask:
    """One queued unit of Forge work.

    Carries every field the Forge Server contract requires: ``task_id``,
    ``project_id``, ``status``, timestamps, ``checkpoint_id``, current
    ``stage``, ``retry_count``, ``result``, and ``error`` — plus the
    concurrency and control fields the workers need.
    """

    task_id: str
    project_id: str
    requirement: str
    status: TaskStatus
    stage: str = ""
    progress: float = 0.0
    priority: int = 0
    mode: str = "assisted"
    actor: str = ""
    version: int = 1
    retry_count: int = 0
    max_retries: int = 2
    checkpoint_id: str = ""
    cancel_requested: bool = False
    pause_requested: bool = False
    available_at: float = 0.0
    result_json: str = "{}"
    error: str = ""
    created_at: float = field(default_factory=now)
    queued_at: "Optional[float]" = None
    started_at: "Optional[float]" = None
    updated_at: float = field(default_factory=now)
    finished_at: "Optional[float]" = None

    # -- derived views ------------------------------------------------------

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def result(self) -> "Dict[str, Any]":
        try:
            payload = json.loads(self.result_json or "{}")
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def timestamps(self) -> "Dict[str, Any]":
        return {
            "created_at": self.created_at,
            "queued_at": self.queued_at,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
        }

    def to_dict(self, *, include_result: bool = False,
                include_requirement: bool = True) -> "Dict[str, Any]":
        payload: "Dict[str, Any]" = {
            "task_id": self.task_id,
            "project_id": self.project_id,
            "status": self.status.value,
            "stage": self.stage,
            "progress": round(float(self.progress), 4),
            "priority": self.priority,
            "mode": self.mode,
            "actor": self.actor,
            "version": self.version,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "checkpoint": self.checkpoint_id,
            "checkpoint_id": self.checkpoint_id,
            "cancel_requested": self.cancel_requested,
            "pause_requested": self.pause_requested,
            "error": self.error,
            "timestamps": self.timestamps(),
            # Flat mirrors keep simple clients simple.
            "created_at": self.created_at,
            "queued_at": self.queued_at,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
        }
        if include_requirement:
            payload["requirement"] = self.requirement
        if include_result:
            payload["result"] = self.result()
        return payload


@dataclass(frozen=True)
class ProjectInfo:
    """A registered project: the only place tasks may touch disk."""

    project_id: str
    name: str
    root: str
    status: str = "active"
    created_at: float = field(default_factory=now)

    def to_dict(self) -> "Dict[str, Any]":
        return {
            "project_id": self.project_id,
            "name": self.name,
            "root": self.root,
            "status": self.status,
            "created_at": self.created_at,
        }


def task_from_row(row: Any) -> ServerTask:
    """Rebuild a task from its SQLite row (fails closed on bad status)."""
    return ServerTask(
        task_id=row["task_id"],
        project_id=row["project_id"],
        requirement=row["requirement"],
        status=TaskStatus(row["status"]),
        stage=row["stage"] or "",
        progress=float(row["progress"] or 0.0),
        priority=int(row["priority"] or 0),
        mode=row["mode"] or "assisted",
        actor=row["actor"] or "",
        version=int(row["version"] or 1),
        retry_count=int(row["retry_count"] or 0),
        max_retries=int(row["max_retries"] or 0),
        checkpoint_id=row["checkpoint_id"] or "",
        cancel_requested=bool(row["cancel_requested"]),
        pause_requested=bool(row["pause_requested"]),
        available_at=float(row["available_at"] or 0.0),
        result_json=row["result_json"] or "{}",
        error=row["error"] or "",
        created_at=float(row["created_at"]),
        queued_at=row["queued_at"],
        started_at=row["started_at"],
        updated_at=float(row["updated_at"]),
        finished_at=row["finished_at"],
    )
