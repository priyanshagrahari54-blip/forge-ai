"""Failure learning (A59): a persistent, bounded failure ledger.

Every pipeline failure (task, agent) is fingerprinted and counted.
Lessons are honest summaries of recurring failures — never invented
fixes. The ledger lives in the plane database, so learning survives
restarts, and it is bounded so it can never grow without limit.
"""
from __future__ import annotations

import time
from typing import Any

MAX_KEYS = 500
MAX_ERROR = 500
MAX_CATEGORY = 16
MAX_LESSON = 140
CATEGORIES = ("task", "stage", "agent", "model", "system")


def fingerprint(error: str) -> str:
    """A stable, bounded key for an error message."""
    return " ".join((error or "").strip().split())[:48].strip().lower()


class FailureLedger:
    """SQLite-backed failure fingerprint ledger."""

    def __init__(self, db: Any) -> None:
        self._db = db
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS failure_events (
                fingerprint TEXT NOT NULL,
                category TEXT NOT NULL,
                error TEXT NOT NULL,
                task_id TEXT NOT NULL DEFAULT '',
                actor TEXT NOT NULL DEFAULT '',
                count INTEGER NOT NULL DEFAULT 1,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                PRIMARY KEY (fingerprint, category)
            )
            """
        )

    def record(self, category: str, error: str, *, task_id: str = "",
               actor: str = "") -> dict[str, Any]:
        category = (category or "").strip().lower()[:MAX_CATEGORY]
        if category not in CATEGORIES:
            raise ValueError(
                f"Unknown failure category {category!r}; expected one "
                f"of {CATEGORIES}")
        error = (error or "").strip()[:MAX_ERROR]
        if not error:
            raise ValueError("error must be non-empty")
        key = fingerprint(error)
        now = time.time()
        row = self._db.query_one(
            "SELECT count, first_seen FROM failure_events "
            "WHERE fingerprint = ? AND category = ?",
            (key, category))
        if row is not None:
            self._db.execute(
                "UPDATE failure_events SET count = count + 1, "
                "last_seen = ?, task_id = ?, actor = ?, error = ? "
                "WHERE fingerprint = ? AND category = ?",
                (now, task_id or "", actor or "", error, key, category))
            count = int(row["count"]) + 1
            first_seen = float(row["first_seen"])
        else:
            self._db.execute(
                "INSERT INTO failure_events (fingerprint, category, "
                "error, task_id, actor, count, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                (key, category, error, task_id or "", actor or "",
                 now, now))
            count = 1
            first_seen = now
        self._prune()
        return {"fingerprint": key, "category": category,
                "error": error, "count": count, "first_seen": first_seen,
                "last_seen": now}

    def _prune(self) -> None:
        """Keep the ledger bounded to the most recently active keys."""
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM failure_events")
        if row is None or int(row["n"]) <= MAX_KEYS:
            return
        self._db.execute(
            "DELETE FROM failure_events WHERE (fingerprint, category) "
            "NOT IN (SELECT fingerprint, category FROM failure_events "
            "ORDER BY last_seen DESC LIMIT ?)",
            (MAX_KEYS,))

    def top(self, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        rows = self._db.query(
            "SELECT fingerprint, category, error, count, first_seen, "
            "last_seen FROM failure_events ORDER BY count DESC, "
            "last_seen DESC LIMIT ?",
            (limit,))
        return [dict(row) for row in rows]

    def lessons(self, limit: int = 5) -> list[dict[str, Any]]:
        return [
            {"category": entry["category"],
             "fingerprint": entry["fingerprint"],
             "count": entry["count"],
             "lesson": (
                 f"recurring {entry['category']} failure "
                 f"({entry['count']}x): "
                 f"{entry['error'][:MAX_LESSON]}")}
            for entry in self.top(limit)
        ]

    def stats(self) -> dict[str, Any]:
        row = self._db.query_one(
            "SELECT COUNT(*) AS keys_n, COALESCE(SUM(count), 0) "
            "AS events_n FROM failure_events")
        return {"distinct_keys": int(row["keys_n"]) if row else 0,
                "total_events": int(row["events_n"]) if row else 0}
