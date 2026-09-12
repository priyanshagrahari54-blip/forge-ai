"""Durable parallel-execution records for the control plane (A81).

One row per submitted parallel execution: the task graph spec, live
status, the agent-activity snapshot, and the final report (per-task
outcomes, structured messages, events). Records are session-scoped at
the API boundary and survive backend restarts in the cockpit database;
the fine-grained execution journal (attempts, every message and event)
lives in the run's :class:`~forge.orchestration.state.ExecutionStateStore`
alongside the project.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

from forge.control.db import Database


class ExecutionStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"


TERMINAL = frozenset({
    ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED,
    ExecutionStatus.PARTIAL, ExecutionStatus.CANCELLED,
})


@dataclass
class Execution:
    id: str
    session_id: str
    project_id: str
    requirement: str
    status: ExecutionStatus
    stage: str
    version: int
    actor: str
    created_at: float
    updated_at: float
    graph_json: str = "{}"
    activity_json: str = "{}"
    report_json: str = "{}"
    error: str = ""
    started_at: float | None = None
    finished_at: float | None = None

    def graph_spec(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.graph_json or "{}")
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def activity(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.activity_json or "{}")
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def report(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.report_json or "{}")
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def to_dict(self, *, include_report: bool = False,
                include_activity: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "execution_id": self.id,
            "project_id": self.project_id,
            "requirement": self.requirement,
            "status": self.status.value,
            "stage": self.stage,
            "version": self.version,
            "actor": self.actor,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "graph": self.graph_spec(),
        }
        if include_activity:
            payload["activity"] = self.activity()
        if include_report:
            payload["report"] = self.report()
        return payload


class ExecutionStore:
    """SQLite-backed execution records with optimistic concurrency."""

    def __init__(self, db: Database) -> None:
        self._db = db
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS executions (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                requirement TEXT NOT NULL,
                status TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                actor TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                graph_json TEXT NOT NULL DEFAULT '{}',
                activity_json TEXT NOT NULL DEFAULT '{}',
                report_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT ''
            )
            """)

    _COLUMNS = ("id", "session_id", "project_id", "requirement", "status",
                "stage", "version", "actor", "created_at", "updated_at",
                "started_at", "finished_at", "graph_json", "activity_json",
                "report_json", "error")

    def create(self, *, session_id: str, project_id: str, requirement: str,
               actor: str, graph_spec: dict[str, Any]) -> Execution:
        now = time.time()
        record = Execution(
            id=uuid.uuid4().hex, session_id=session_id,
            project_id=project_id, requirement=requirement,
            status=ExecutionStatus.QUEUED, stage="queued", version=1,
            actor=actor, created_at=now, updated_at=now,
            graph_json=json.dumps(graph_spec, default=str))
        self._db.execute(
            "INSERT INTO executions ("
            " id, session_id, project_id, requirement, status, stage,"
            " version, actor, created_at, updated_at, graph_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record.id, record.session_id, record.project_id,
             record.requirement, record.status.value, record.stage,
             record.version, record.actor, record.created_at,
             record.updated_at, record.graph_json))
        return record

    def get(self, execution_id: str) -> Execution | None:
        row = self._db.execute(
            "SELECT * FROM executions WHERE id = ?", (execution_id,)
        ).fetchone()
        return self._from_row(row) if row else None

    def list_for_session(self, session_id: str) -> list[Execution]:
        rows = self._db.execute(
            "SELECT * FROM executions WHERE session_id = ?"
            " ORDER BY created_at DESC LIMIT 200", (session_id,)).fetchall()
        return [self._from_row(row) for row in rows]

    def latest_for_project(self, project_id: str) -> Execution | None:
        row = self._db.execute(
            "SELECT * FROM executions WHERE project_id = ?"
            " ORDER BY created_at DESC LIMIT 1", (project_id,)).fetchone()
        return self._from_row(row) if row else None

    def compare_and_set(self, execution_id: str, version: int,
                        **updates: Any) -> Execution | None:
        if not updates:
            return self.get(execution_id)
        fields = [column for column in self._COLUMNS if column in updates]
        if not fields:
            return self.get(execution_id)
        values = [updates[column] for column in fields]
        assignments = ", ".join(f"{column} = ?" for column in fields) \
            + ", updated_at = ?"
        params = (*values, time.time(), execution_id, version)
        cursor = self._db.execute(
            f"UPDATE executions SET {assignments}"
            " WHERE id = ? AND version = ?", params)
        if cursor.rowcount == 0:
            return None
        return self.get(execution_id)

    def mutate(self, execution_id: str, **updates: Any) -> Execution | None:
        record = self.get(execution_id)
        if record is None:
            return None
        return self.compare_and_set(execution_id, record.version,
                                    **updates)

    @staticmethod
    def _from_row(row: tuple) -> Execution:
        columns = ExecutionStore._COLUMNS
        values = dict(zip(columns, row))
        return Execution(
            id=values["id"], session_id=values["session_id"],
            project_id=values["project_id"],
            requirement=values["requirement"],
            status=ExecutionStatus(values["status"]),
            stage=values["stage"], version=values["version"],
            actor=values["actor"], created_at=values["created_at"],
            updated_at=values["updated_at"],
            started_at=values["started_at"],
            finished_at=values["finished_at"],
            graph_json=values["graph_json"],
            activity_json=values["activity_json"],
            report_json=values["report_json"],
            error=values["error"])
