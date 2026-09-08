"""Agent memory (A53): durable, policy-gated memory per agent.

Memories are simple key/value facts scoped to one runtime-defined
agent. The plane gates every read/write behind ``Resource.MEMORY``
with scope ``agent:{name}`` — DENY fails closed and nothing is ever
stored silently.
"""
from __future__ import annotations

import time
from typing import Any

MAX_ENTRIES = 200
MAX_KEY = 64
MAX_VALUE = 2000


class AgentMemoryStore:
    """SQLite-backed per-agent memory."""

    def __init__(self, db: Any) -> None:
        self._db = db
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_memory (
                agent_name TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (agent_name, key)
            )
            """
        )

    def set(self, agent_name: str, key: str, value: str) -> dict[str, Any]:
        key = (key or "").strip()[:MAX_KEY]
        value = (value or "").strip()[:MAX_VALUE]
        if not key:
            raise ValueError("key must be non-empty")
        count_row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM agent_memory "
            "WHERE agent_name = ? AND key != ?",
            (agent_name, key))
        if count_row and int(count_row["n"]) >= MAX_ENTRIES:
            raise ValueError(f"memory limit reached ({MAX_ENTRIES})")
        now = time.time()
        self._db.execute(
            "INSERT OR REPLACE INTO agent_memory "
            "(agent_name, key, value, updated_at) VALUES (?, ?, ?, ?)",
            (agent_name, key, value, now))
        return {"agent": agent_name, "key": key, "value": value,
                "updated_at": now}

    def get(self, agent_name: str, key: str) -> dict[str, Any] | None:
        row = self._db.query_one(
            "SELECT key, value, updated_at FROM agent_memory "
            "WHERE agent_name = ? AND key = ?",
            (agent_name, (key or "").strip()[:MAX_KEY]))
        if row is None:
            return None
        return {"agent": agent_name, "key": row["key"],
                "value": row["value"],
                "updated_at": float(row["updated_at"])}

    def list(self, agent_name: str) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT key, value, updated_at FROM agent_memory "
            "WHERE agent_name = ? ORDER BY updated_at DESC",
            (agent_name,))
        return [{"agent": agent_name, "key": row["key"],
                 "value": row["value"],
                 "updated_at": float(row["updated_at"])}
                for row in rows]

    def delete(self, agent_name: str, key: str) -> bool:
        before = self.get(agent_name, key)
        if before is None:
            return False
        self._db.execute(
            "DELETE FROM agent_memory WHERE agent_name = ? AND key = ?",
            (agent_name, (key or "").strip()[:MAX_KEY]))
        return True
