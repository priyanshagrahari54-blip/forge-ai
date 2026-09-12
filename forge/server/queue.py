"""The persistent task queue (A81).

The queue lives in SQLite (``queue_items``), never in process memory, so
it survives restarts: queued work is still queued after a crash, and a
worker that died mid-lease is detected at startup by
:meth:`TaskQueue.recover`, which clears every stale lease so the
scheduler can hand the work to a live worker.

Leases are the crash-safety primitive. A worker holds a lease for the
duration of one execution and re-validates it before committing results
(:meth:`lease_held_by`); after a restart the lease is gone, so a zombie
worker from the previous process can never overwrite fresh state.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from forge.server.storage import Database


class TaskQueue:
    """SQLite-backed priority queue with crash-recoverable leases."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._lock = threading.Lock()

    # -- enqueue / remove ------------------------------------------------------

    def enqueue(self, task_id: str, project_id: str, *,
                priority: int = 0, available_at: float = 0.0) -> bool:
        """Add (or refresh) a queue item. Idempotent per task."""
        cursor = self._db.execute(
            "INSERT INTO queue_items (task_id, project_id, priority, "
            "enqueued_at, available_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET "
            "priority = excluded.priority, "
            "available_at = excluded.available_at, "
            "leased_at = NULL, lease_owner = ''",
            (task_id, project_id, int(priority), time.time(),
             float(available_at)))
        return cursor.rowcount > 0

    def remove(self, task_id: str) -> bool:
        cursor = self._db.execute(
            "DELETE FROM queue_items WHERE task_id = ?", (task_id,))
        return cursor.rowcount > 0

    def requeue(self, task_id: str, *, priority: Optional[int] = None,
                available_at: float = 0.0) -> None:
        """Clear a lease and make the item available again."""
        if priority is None:
            self._db.execute(
                "UPDATE queue_items SET leased_at = NULL, lease_owner = '', "
                "available_at = ? WHERE task_id = ?",
                (float(available_at), task_id))
        else:
            self._db.execute(
                "UPDATE queue_items SET leased_at = NULL, lease_owner = '', "
                "available_at = ?, priority = ? WHERE task_id = ?",
                (float(available_at), int(priority), task_id))

    # -- leasing -----------------------------------------------------------------

    def lease_next(self, project_id: str, owner: str, *,
                   now: Optional[float] = None) -> Optional[str]:
        """Atomically lease the best available item for one project.

        Returns the task id, or ``None`` when nothing is dispatchable.
        Ordering: priority (desc), then FIFO by enqueue time. Items with
        ``available_at`` in the future (retry backoff) are skipped.
        """
        moment = time.time() if now is None else now
        with self._lock:
            for _attempt in range(5):
                row = self._db.query_one(
                    "SELECT task_id FROM queue_items WHERE project_id = ? "
                    "AND leased_at IS NULL AND available_at <= ? "
                    "ORDER BY priority DESC, enqueued_at ASC LIMIT 1",
                    (project_id, moment))
                if row is None:
                    return None
                task_id = str(row["task_id"])
                cursor = self._db.execute(
                    "UPDATE queue_items SET leased_at = ?, lease_owner = ? "
                    "WHERE task_id = ? AND leased_at IS NULL",
                    (moment, owner, task_id))
                if cursor.rowcount == 1:
                    return task_id
                # Lost the race to another dispatcher tick; retry.
            return None

    def release(self, task_id: str) -> None:
        """Drop the queue item after the task reached a terminal state."""
        self.remove(task_id)

    def lease_held_by(self, task_id: str, owner: str) -> bool:
        """Fencing check: is this owner still the legitimate lease holder?"""
        row = self._db.query_one(
            "SELECT lease_owner, leased_at FROM queue_items "
            "WHERE task_id = ?", (task_id,))
        return (row is not None and row["leased_at"] is not None
                and str(row["lease_owner"]) == owner)

    # -- recovery / introspection --------------------------------------------------

    def recover(self) -> int:
        """Clear every lease (startup): dead workers hold nothing."""
        cursor = self._db.execute(
            "UPDATE queue_items SET leased_at = NULL, lease_owner = '' "
            "WHERE leased_at IS NOT NULL")
        return cursor.rowcount

    def depth(self, project_id: str = "") -> int:
        if project_id:
            row = self._db.query_one(
                "SELECT COUNT(*) AS n FROM queue_items WHERE project_id = ?",
                (project_id,))
        else:
            row = self._db.query_one(
                "SELECT COUNT(*) AS n FROM queue_items")
        return int(row["n"]) if row else 0

    def leased_count(self) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM queue_items WHERE leased_at IS NOT NULL")
        return int(row["n"]) if row else 0

    def dispatchable_count(self, project_id: str = "", *,
                           now: Optional[float] = None) -> int:
        """Items a dispatcher could lease right now (not in backoff)."""
        moment = time.time() if now is None else now
        sql = ("SELECT COUNT(*) AS n FROM queue_items WHERE leased_at "
               "IS NULL AND available_at <= ?")
        params: List[Any] = [moment]
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        row = self._db.query_one(sql, params)
        return int(row["n"]) if row else 0

    def peek(self, project_id: str = "", limit: int = 20) -> List[Dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        sql = ("SELECT task_id, project_id, priority, enqueued_at, "
               "available_at, leased_at, lease_owner FROM queue_items")
        params: List[Any] = []
        if project_id:
            sql += " WHERE project_id = ?"
            params.append(project_id)
        sql += " ORDER BY priority DESC, enqueued_at ASC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self._db.query(sql, params)]

    def contains(self, task_id: str) -> bool:
        row = self._db.query_one(
            "SELECT 1 AS one FROM queue_items WHERE task_id = ?", (task_id,))
        return row is not None

    def position(self, task_id: str) -> int:
        """1-based dispatch position within the task's project (0: absent)."""
        item = self._db.query_one(
            "SELECT project_id, priority, enqueued_at FROM queue_items "
            "WHERE task_id = ?", (task_id,))
        if item is None:
            return 0
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM queue_items WHERE project_id = ? "
            "AND (priority > ? OR (priority = ? AND enqueued_at <= ?))",
            (item["project_id"], item["priority"], item["priority"],
             item["enqueued_at"]))
        return int(row["n"]) if row else 0
