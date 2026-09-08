"""Session-scoped durable memory for the control plane (A37).

Session memory is the *remembered* layer of a cockpit session: notes,
facts, and summaries that survive restarts because they live in the same
SQLite database as sessions and runs. Entries are session-scoped (one
session can never read another's), bounded per entry and per session,
and always served redacted at the API boundary.

Project-wide durable knowledge (facts, decisions, learned performance)
lives in :class:`forge.memory.MemoryStore`; run outcomes are recorded
there as bounded summaries.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from forge.control.db import Database

MAX_ENTRIES_PER_SESSION = 500
MAX_CONTENT_BYTES = 20_000
MAX_TOTAL_BYTES_PER_SESSION = 1_000_000
VALID_KINDS = ("note", "fact", "summary")


@dataclass(frozen=True)
class MemoryEntry:
    id: str
    session_id: str
    kind: str
    content: str
    source: str
    created_at: float

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "session_id": self.session_id,
            "kind": self.kind,
            "source": self.source,
            "created_at": self.created_at,
        }
        if include_content:
            payload["content"] = self.content
        return payload


class SessionMemoryStore:
    """SQLite-backed, bounded, session-scoped memory entries."""

    def __init__(self, db: Database) -> None:
        self._db = db
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS session_memory (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )
            """)
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_memory_session "
            "ON session_memory(session_id, created_at)")

    def add(self, session_id: str, kind: str, content: str, *,
            source: str = "") -> MemoryEntry:
        """Store one bounded entry; prunes oldest entries beyond bounds."""
        if not session_id:
            raise ValueError("session_id is required for session memory")
        if kind not in VALID_KINDS:
            raise ValueError(
                f"Memory kind must be one of {VALID_KINDS}: {kind!r}")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Memory content must be a non-empty string")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_CONTENT_BYTES:
            raise ValueError(
                f"Memory entry exceeds the {MAX_CONTENT_BYTES}-byte bound")
        entry = MemoryEntry(
            id=uuid.uuid4().hex, session_id=session_id, kind=kind,
            content=content, source=source[:128], created_at=time.time())
        self._db.execute(
            "INSERT INTO session_memory (id, session_id, kind, content, "
            "source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (entry.id, entry.session_id, entry.kind, entry.content,
             entry.source, entry.created_at))
        self._prune(session_id)
        return entry

    def list(self, session_id: str) -> list[MemoryEntry]:
        """Newest first, session-scoped."""
        rows = self._db.query(
            "SELECT id, session_id, kind, content, source, created_at "
            "FROM session_memory WHERE session_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (session_id, MAX_ENTRIES_PER_SESSION))
        return [self._from_row(row) for row in rows]

    def get(self, session_id: str, entry_id: str) -> MemoryEntry | None:
        row = self._db.query_one(
            "SELECT id, session_id, kind, content, source, created_at "
            "FROM session_memory WHERE id = ? AND session_id = ?",
            (entry_id, session_id))
        return self._from_row(row) if row is not None else None

    def delete(self, session_id: str, entry_id: str) -> bool:
        cursor = self._db.execute(
            "DELETE FROM session_memory WHERE id = ? AND session_id = ?",
            (entry_id, session_id))
        return cursor.rowcount > 0

    def total_bytes(self, session_id: str) -> int:
        row = self._db.query_one(
            "SELECT COALESCE(SUM(LENGTH(content)), 0) AS total "
            "FROM session_memory WHERE session_id = ?", (session_id,))
        return int(row["total"]) if row is not None else 0

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _from_row(row: Any) -> MemoryEntry:
        return MemoryEntry(
            id=row["id"], session_id=row["session_id"], kind=row["kind"],
            content=row["content"], source=row["source"],
            created_at=row["created_at"])

    def _prune(self, session_id: str) -> None:
        """FIFO pruning: oldest entries go first when bounds are crossed."""
        rows = self._db.query(
            "SELECT id FROM session_memory WHERE session_id = ? "
            "ORDER BY created_at ASC, id ASC", (session_id,))
        keep = MAX_ENTRIES_PER_SESSION
        while len(rows) > keep:
            victim = rows.pop(0)
            self._db.execute(
                "DELETE FROM session_memory WHERE id = ? AND "
                "session_id = ?", (victim["id"], session_id))
        while self.total_bytes(session_id) > MAX_TOTAL_BYTES_PER_SESSION \
                and rows:
            victim = rows.pop(0)
            self._db.execute(
                "DELETE FROM session_memory WHERE id = ? AND "
                "session_id = ?", (victim["id"], session_id))
