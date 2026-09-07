"""Persistent task event log with cursor replay and live wait (A34).

Every event is appended to SQLite (survives browser refresh and API
restart) and fanned out to live subscribers. Reconnecting browsers pass
``after=<seq>`` and receive exactly the events they missed — sequences
are monotonic per task and never reused, so historical + live streams
reconcile without duplicates.

Payloads pass through :func:`forge.core.report.redact`, so secrets can
never land in the event stream.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from forge.control.db import Database
from forge.core.report import redact

#: Hard cap on one event payload; larger details are truncated, never stored.
MAX_EVENT_BYTES = 64 * 1024


@dataclass(frozen=True)
class StoredEvent:
    seq: int
    event_id: str
    task_id: str
    project_id: str
    timestamp: float
    type: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "task_id": self.task_id,
            "timestamp": self.timestamp,
            "type": self.type,
            "data": dict(self.data),
        }


class EventStore:
    """SQLite-backed, per-task sequenced event log."""

    def __init__(self, db: Database, *,
                 max_events_per_task: int = 5000) -> None:
        self._db = db
        self.max_events_per_task = max(100, max_events_per_task)
        self._cond = threading.Condition()
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                type TEXT NOT NULL,
                data TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_task_seq "
            "ON events(task_id, seq)"
        )

    def append(self, task_id: str, project_id: str, event_type: str,
               data: dict[str, Any] | None = None) -> StoredEvent:
        safe = redact(dict(data or {}))
        encoded = json.dumps(safe, default=str)
        if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
            clipped = {"truncated": True,
                       "preview": encoded[:MAX_EVENT_BYTES // 2]}
            encoded = json.dumps(clipped)
            safe = clipped
        event = StoredEvent(
            seq=0, event_id=uuid4().hex, task_id=task_id,
            project_id=project_id, timestamp=time.time(),
            type=event_type, data=safe)
        with self._cond:
            cursor = self._db.execute(
                "INSERT INTO events (event_id, task_id, project_id, "
                "timestamp, type, data) VALUES (?, ?, ?, ?, ?, ?)",
                (event.event_id, event.task_id, event.project_id,
                 event.timestamp, event.type, encoded))
            stored = StoredEvent(
                seq=int(cursor.lastrowid or 0), event_id=event.event_id,
                task_id=event.task_id, project_id=event.project_id,
                timestamp=event.timestamp, type=event.type, data=safe)
            self._prune_locked(task_id)
            self._cond.notify_all()
            return stored

    def _prune_locked(self, task_id: str) -> None:
        """Keep the per-task buffer bounded; sequences stay monotonic."""
        self._db.execute(
            """
            DELETE FROM events WHERE task_id = ? AND seq NOT IN (
                SELECT seq FROM events WHERE task_id = ?
                ORDER BY seq DESC LIMIT ?
            )
            """,
            (task_id, task_id, self.max_events_per_task))

    def _row_to_event(self, row: Any) -> StoredEvent:
        try:
            data = json.loads(row["data"] or "{}")
        except ValueError:
            data = {"corrupt": True}
        if not isinstance(data, dict):
            data = {"value": data}
        return StoredEvent(
            seq=int(row["seq"]), event_id=row["event_id"],
            task_id=row["task_id"], project_id=row["project_id"],
            timestamp=float(row["timestamp"]), type=row["type"],
            data=data)

    def list(self, task_id: str, *, after: int = 0,
             limit: int = 200) -> tuple[list[StoredEvent], int]:
        """Events with ``seq > after``; returns (events, latest seq)."""
        limit = max(1, min(500, int(limit)))
        after = max(0, int(after))
        rows = self._db.query(
            "SELECT * FROM events WHERE task_id = ? AND seq > ? "
            "ORDER BY seq ASC LIMIT ?",
            (task_id, after, limit))
        events = [self._row_to_event(row) for row in rows]
        latest = self.latest_seq(task_id)
        return events, latest

    def latest_seq(self, task_id: str) -> int:
        row = self._db.query_one(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM events "
            "WHERE task_id = ?",
            (task_id,))
        return int(row["m"]) if row else 0

    def wait(self, task_id: str, after: int,
             timeout: float = 25.0) -> list[StoredEvent]:
        """Block until events past ``after`` exist, or timeout.

        Returns immediately when events are already available. Used by the
        live event stream; spurious wakeups just re-check.
        """
        deadline = time.time() + max(0.1, timeout)
        with self._cond:
            while True:
                rows = self._db.query(
                    "SELECT * FROM events WHERE task_id = ? AND seq > ? "
                    "ORDER BY seq ASC LIMIT 200",
                    (task_id, max(0, int(after))))
                if rows:
                    return [self._row_to_event(row) for row in rows]
                remaining = deadline - time.time()
                if remaining <= 0:
                    return []
                self._cond.wait(timeout=min(remaining, 5.0))

    def recent_for_project(self, project_id: str,
                           limit: int = 50) -> list[StoredEvent]:
        rows = self._db.query(
            "SELECT * FROM events WHERE project_id = ? "
            "ORDER BY seq DESC LIMIT ?",
            (project_id, max(1, min(200, int(limit)))))
        return [self._row_to_event(row) for row in rows]
