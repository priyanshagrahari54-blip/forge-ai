"""Persistent notifications (A81).

Notifications are the server's durable "you should look at this" list:
task completion/failure/cancellation, approval requests, and recovery
actions. They persist in SQLite, are bounded per project, and carry an
unread flag — so a Forge Desktop client that was offline when a task
finished still learns about it on reconnect (the recovery bundle
includes unread notifications).
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional
from uuid import uuid4

from forge.core.report import redact_text
from forge.server.storage import Database

#: Bounded retention per project; oldest are pruned first.
MAX_NOTIFICATIONS_PER_PROJECT = 500


class NotificationService:
    """SQLite-backed notifications with unread tracking."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._lock = threading.Lock()

    def notify(self, project_id: str, kind: str, title: str,
               body: str = "", *, task_id: str = "") -> str:
        identifier = uuid4().hex
        with self._lock:
            self._db.execute(
                "INSERT INTO notifications (id, project_id, task_id, kind, "
                "title, body, created_at, read_at) VALUES (?, ?, ?, ?, ?, "
                "?, ?, NULL)",
                (identifier, project_id, task_id, str(kind)[:64],
                 redact_text(str(title))[:200],
                 redact_text(str(body))[:1000], time.time()))
            self._prune_locked(project_id)
        return identifier

    def _prune_locked(self, project_id: str) -> None:
        self._db.execute(
            "DELETE FROM notifications WHERE project_id = ? AND id NOT IN ("
            " SELECT id FROM notifications WHERE project_id = ?"
            " ORDER BY created_at DESC LIMIT ?)",
            (project_id, project_id, MAX_NOTIFICATIONS_PER_PROJECT))

    def list(self, project_id: str = "", *, unread_only: bool = False,
             limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        sql = "SELECT * FROM notifications"
        clauses: List[str] = []
        params: List[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if unread_only:
            clauses.append("read_at IS NULL")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [self._row_to_dict(row)
                for row in self._db.query(sql, params)]

    def mark_read(self, notification_id: str) -> bool:
        cursor = self._db.execute(
            "UPDATE notifications SET read_at = ? WHERE id = ? "
            "AND read_at IS NULL", (time.time(), notification_id))
        return cursor.rowcount > 0

    def mark_all_read(self, project_id: str = "") -> int:
        if project_id:
            cursor = self._db.execute(
                "UPDATE notifications SET read_at = ? WHERE project_id = ? "
                "AND read_at IS NULL", (time.time(), project_id))
        else:
            cursor = self._db.execute(
                "UPDATE notifications SET read_at = ? WHERE read_at IS NULL",
                (time.time(),))
        return cursor.rowcount

    def unread_count(self, project_id: str = "") -> int:
        if project_id:
            row = self._db.query_one(
                "SELECT COUNT(*) AS n FROM notifications "
                "WHERE project_id = ? AND read_at IS NULL", (project_id,))
        else:
            row = self._db.query_one(
                "SELECT COUNT(*) AS n FROM notifications "
                "WHERE read_at IS NULL")
        return int(row["n"]) if row else 0

    @staticmethod
    def _row_to_dict(row: Any) -> Dict[str, Any]:
        return {
            "notification_id": row["id"],
            "project_id": row["project_id"],
            "task_id": row["task_id"],
            "kind": row["kind"],
            "title": row["title"],
            "body": row["body"],
            "created_at": row["created_at"],
            "read_at": row["read_at"],
            "unread": row["read_at"] is None,
        }
