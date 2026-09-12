"""Persistent server event log with cursor replay and live wait (A81).

Every lifecycle transition, stage change, approval, and worker message
is appended to SQLite and fanned out to live waiters. Reconnecting
clients pass ``after=<seq>`` and receive exactly the events they missed;
sequences are globally monotonic and never reused, so historical and
live streams reconcile without gaps or duplicates.

Payloads pass through :func:`forge.core.report.redact`, so credentials
can never land in the event stream, and each event is size-capped.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from forge.core.report import redact
from forge.server.storage import Database

#: Hard cap on one event payload; larger details are truncated, never dropped
#: silently — the truncation is recorded in the stored event.
MAX_EVENT_BYTES = 64 * 1024


@dataclass(frozen=True)
class ServerEvent:
    seq: int
    event_id: str
    task_id: str
    project_id: str
    timestamp: float
    type: str
    data: "Dict[str, Any]"

    def to_dict(self) -> "Dict[str, Any]":
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "timestamp": self.timestamp,
            "type": self.type,
            "data": dict(self.data),
        }


class EventStore:
    """SQLite-backed, sequenced event log with live-wait support."""

    def __init__(self, db: Database, *,
                 max_events_per_task: int = 5000) -> None:
        self._db = db
        self.max_events_per_task = max(100, int(max_events_per_task))
        self._cond = threading.Condition()
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_task_seq "
            "ON events(task_id, seq)")

    def append(self, task_id: str, project_id: str, event_type: str,
               data: "Optional[Dict[str, Any]]" = None) -> ServerEvent:
        safe = redact(dict(data or {}))
        encoded = json.dumps(safe, default=str)
        if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
            clipped = {"truncated": True,
                       "preview": encoded[:MAX_EVENT_BYTES // 2]}
            encoded = json.dumps(clipped)
            safe = clipped
        with self._cond:
            event_id = uuid4().hex
            timestamp = time.time()
            cursor = self._db.execute(
                "INSERT INTO events (event_id, task_id, project_id, "
                "timestamp, type, data) VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, task_id, project_id, timestamp,
                 event_type, encoded))
            stored = ServerEvent(
                seq=int(cursor.lastrowid or 0), event_id=event_id,
                task_id=task_id, project_id=project_id,
                timestamp=timestamp, type=event_type, data=safe)
            self._prune_locked(task_id)
            self._cond.notify_all()
            return stored

    def _prune_locked(self, task_id: str) -> None:
        """Keep the per-task buffer bounded; sequences stay monotonic."""
        self._db.execute(
            "DELETE FROM events WHERE task_id = ? AND seq NOT IN ("
            " SELECT seq FROM events WHERE task_id = ?"
            " ORDER BY seq DESC LIMIT ?)",
            (task_id, task_id, self.max_events_per_task))

    def _row_to_event(self, row: Any) -> ServerEvent:
        try:
            data = json.loads(row["data"] or "{}")
        except ValueError:
            data = {"corrupt": True}
        if not isinstance(data, dict):
            data = {"value": data}
        return ServerEvent(
            seq=int(row["seq"]), event_id=row["event_id"],
            task_id=row["task_id"], project_id=row["project_id"],
            timestamp=float(row["timestamp"]), type=row["type"], data=data)

    def list(self, task_id: str, *, after: int = 0,
             limit: int = 200) -> "Tuple[List[ServerEvent], int]":
        """Events with ``seq > after``; returns (events, latest seq)."""
        limit = max(1, min(500, int(limit)))
        after = max(0, int(after))
        rows = self._db.query(
            "SELECT * FROM events WHERE task_id = ? AND seq > ? "
            "ORDER BY seq ASC LIMIT ?", (task_id, after, limit))
        return [self._row_to_event(row) for row in rows], \
            self.latest_seq(task_id)

    def latest_seq(self, task_id: str = "") -> int:
        if task_id:
            row = self._db.query_one(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM events "
                "WHERE task_id = ?", (task_id,))
        else:
            row = self._db.query_one(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM events")
        return int(row["m"]) if row else 0

    def wait(self, task_id: str, after: int,
             timeout: float = 25.0) -> "List[ServerEvent]":
        """Block until events past ``after`` exist, or the timeout elapses.

        Used by the live-update long poll; returns immediately when
        events are already available.
        """
        deadline = time.time() + max(0.1, float(timeout))
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
                           limit: int = 50) -> "List[ServerEvent]":
        rows = self._db.query(
            "SELECT * FROM events WHERE project_id = ? "
            "ORDER BY seq DESC LIMIT ?",
            (project_id, max(1, min(200, int(limit)))))
        return [self._row_to_event(row) for row in rows]
