"""Durable orchestration records for the control plane (A38).

One row per submitted multi-agent orchestration: plan, live status,
stage, and the final report (per-step outcomes with evidence). Records
are session-scoped at the API boundary and survive backend restarts
because they live in the cockpit database.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

from forge.control.db import Database


class OrchestrationStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL = frozenset({
    OrchestrationStatus.SUCCEEDED, OrchestrationStatus.FAILED,
    OrchestrationStatus.CANCELLED,
})


@dataclass
class Orchestration:
    id: str
    session_id: str
    project_id: str
    requirement: str
    status: OrchestrationStatus
    stage: str
    version: int
    actor: str
    created_at: float
    updated_at: float
    started_at: float | None = None
    finished_at: float | None = None
    plan_json: str = "{}"
    report_json: str = "{}"
    error: str = ""

    def plan(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.plan_json or "{}")
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def report(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.report_json or "{}")
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def to_dict(self, *, include_report: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "orchestration_id": self.id,
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
            "plan": self.plan(),
        }
        if include_report:
            payload["report"] = self.report()
        return payload


class OrchestrationStore:
    """SQLite-backed orchestration records with optimistic concurrency."""

    def __init__(self, db: Database) -> None:
        self._db = db
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS orchestrations (
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
                plan_json TEXT NOT NULL DEFAULT '{}',
                report_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT ''
            )
            """)

    _COLUMNS = ("id", "session_id", "project_id", "requirement", "status",
                "stage", "version", "actor", "created_at", "updated_at",
                "started_at", "finished_at", "plan_json", "report_json",
                "error")

    def create(self, *, session_id: str, project_id: str,
               requirement: str, actor: str) -> Orchestration:
        now = time.time()
        record = Orchestration(
            id=uuid.uuid4().hex, session_id=session_id,
            project_id=project_id, requirement=requirement,
            status=OrchestrationStatus.QUEUED, stage="queued", version=1,
            actor=actor, created_at=now, updated_at=now)
        self._db.execute(
            "INSERT INTO orchestrations ("
            " id, session_id, project_id, requirement, status, stage,"
            " version, actor, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record.id, record.session_id, record.project_id,
             record.requirement, record.status.value, record.stage,
             record.version, record.actor, record.created_at,
             record.updated_at))
        return record

    def get(self, orchestration_id: str) -> Orchestration | None:
        row = self._db.execute(
            "SELECT * FROM orchestrations WHERE id = ?",
            (orchestration_id,)).fetchone()
        return self._from_row(row) if row else None

    def list_for_session(self, session_id: str) -> list[Orchestration]:
        rows = self._db.execute(
            "SELECT * FROM orchestrations WHERE session_id = ?"
            " ORDER BY created_at DESC LIMIT 200",
            (session_id,)).fetchall()
        return [self._from_row(row) for row in rows]

    def compare_and_set(self, orchestration_id: str, version: int,
                        **updates: Any) -> Orchestration | None:
        """Optimistic update; returns the updated record or ``None``."""
        if not updates:
            record = self.get(orchestration_id)
            return record
        fields = [column for column in self._COLUMNS if column in updates]
        if not fields:
            return self.get(orchestration_id)
        values = [updates[column] for column in fields]
        assignments = ", ".join(
            f"{column} = ?" for column in fields) + ", updated_at = ?"
        params = (*values, time.time(), orchestration_id, version)
        cursor = self._db.execute(
            f"UPDATE orchestrations SET {assignments}"
            " WHERE id = ? AND version = ?", params)
        if cursor.rowcount == 0:
            return None
        return self.get(orchestration_id)

    def mutate(self, orchestration_id: str, **updates: Any) -> Orchestration | None:
        record = self.get(orchestration_id)
        if record is None:
            return None
        return self.compare_and_set(orchestration_id, record.version,
                                    **updates)

    @staticmethod
    def _from_row(row: tuple) -> Orchestration:
        columns = ("id", "session_id", "project_id", "requirement", "status",
                   "stage", "version", "actor", "created_at", "updated_at",
                   "started_at", "finished_at", "plan_json", "report_json",
                   "error")
        values = dict(zip(columns, row))
        return Orchestration(
            id=values["id"], session_id=values["session_id"],
            project_id=values["project_id"],
            requirement=values["requirement"],
            status=OrchestrationStatus(values["status"]),
            stage=values["stage"], version=values["version"],
            actor=values["actor"], created_at=values["created_at"],
            updated_at=values["updated_at"],
            started_at=values["started_at"],
            finished_at=values["finished_at"],
            plan_json=values["plan_json"], report_json=values["report_json"],
            error=values["error"])
