"""Persistent, per-task log storage (A81).

Logs are the human-readable trace of one task: worker messages, stage
notes, executor diagnostics, and recovery actions. They are stored in
SQLite (bounded per task), redacted before storage, and addressable by
row id so a reconnecting client can page with ``after=<id>`` and never
re-read or miss a line.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from forge.core.report import redact_text
from forge.server.storage import Database

#: Hard cap on one log line.
MAX_LOG_CHARS = 4000

LEVELS = frozenset({"debug", "info", "warning", "error"})


@dataclass(frozen=True)
class LogEntry:
    id: int
    task_id: str
    project_id: str
    timestamp: float
    level: str
    source: str
    message: str

    def to_dict(self) -> "Dict[str, Any]":
        return {
            "id": self.id,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "timestamp": self.timestamp,
            "level": self.level,
            "source": self.source,
            "message": self.message,
        }


class LogStore:
    """SQLite-backed task logs with a bounded per-task buffer."""

    def __init__(self, db: Database, *,
                 max_logs_per_task: int = 2000) -> None:
        self._db = db
        self.max_logs_per_task = max(50, int(max_logs_per_task))
        self._lock = threading.Lock()

    def append(self, task_id: str, project_id: str, message: str, *,
               level: str = "info", source: str = "") -> LogEntry:
        level = level if level in LEVELS else "info"
        safe = redact_text(str(message))[:MAX_LOG_CHARS]
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO logs (task_id, project_id, timestamp, level, "
                "source, message) VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, project_id, time.time(), level,
                 str(source)[:64], safe))
            entry_id = int(cursor.lastrowid or 0)
            self._prune_locked(task_id)
        return LogEntry(
            id=entry_id, task_id=task_id, project_id=project_id,
            timestamp=time.time(), level=level, source=str(source)[:64],
            message=safe)

    def _prune_locked(self, task_id: str) -> None:
        self._db.execute(
            "DELETE FROM logs WHERE task_id = ? AND id NOT IN ("
            " SELECT id FROM logs WHERE task_id = ?"
            " ORDER BY id DESC LIMIT ?)",
            (task_id, task_id, self.max_logs_per_task))

    def list(self, task_id: str, *, after: int = 0, level: str = "",
             limit: int = 200) -> "Tuple[List[LogEntry], int]":
        """Log lines with ``id > after``; returns (entries, latest id)."""
        limit = max(1, min(1000, int(limit)))
        sql = "SELECT * FROM logs WHERE task_id = ? AND id > ?"
        params: List[Any] = [task_id, max(0, int(after))]
        if level in LEVELS:
            sql += " AND level = ?"
            params.append(level)
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        rows = self._db.query(sql, params)
        latest_row = self._db.query_one(
            "SELECT COALESCE(MAX(id), 0) AS m FROM logs WHERE task_id = ?",
            (task_id,))
        latest = int(latest_row["m"]) if latest_row else 0
        return [self._row_to_entry(row) for row in rows], latest

    def tail(self, task_id: str, limit: int = 100) -> "List[LogEntry]":
        limit = max(1, min(1000, int(limit)))
        rows = self._db.query(
            "SELECT * FROM (SELECT * FROM logs WHERE task_id = ? "
            "ORDER BY id DESC LIMIT ?) ORDER BY id ASC", (task_id, limit))
        return [self._row_to_entry(row) for row in rows]

    @staticmethod
    def _row_to_entry(row: Any) -> LogEntry:
        return LogEntry(
            id=int(row["id"]), task_id=row["task_id"],
            project_id=row["project_id"], timestamp=float(row["timestamp"]),
            level=row["level"], source=row["source"] or "",
            message=row["message"] or "")
