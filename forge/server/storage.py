"""SQLite storage foundation for the Forge Server (A81).

One database file holds everything the server must survive across
restarts: tasks, the work queue, events, logs, sessions, API keys,
projects, approvals, notifications, and server state. A single
connection guarded by a lock keeps the implementation small and
thread-safe; WAL mode plus a busy timeout allows a second process (or a
restarting server) to open the same file without spurious lock errors.

SQLite is the initial backend by design: zero-configuration, durable,
and available on every supported platform (Python 3.8+, Windows
included). All server stores take this :class:`Database` so a different
engine can replace it later behind one seam.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

#: Schema version, persisted in ``server_state`` for forward migrations.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    requirement TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    progress REAL NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'assisted',
    actor TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 2,
    checkpoint_id TEXT NOT NULL DEFAULT '',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    pause_requested INTEGER NOT NULL DEFAULT 0,
    available_at REAL NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    queued_at REAL,
    started_at REAL,
    updated_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_project_status
    ON tasks(project_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at);

CREATE TABLE IF NOT EXISTS queue_items (
    task_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    enqueued_at REAL NOT NULL,
    available_at REAL NOT NULL DEFAULT 0,
    leased_at REAL,
    lease_owner TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_queue_project
    ON queue_items(project_id, priority DESC, enqueued_at ASC);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    type TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_task_seq ON events(task_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id, seq);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    source TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_task ON logs(task_id, id);

CREATE TABLE IF NOT EXISTS api_keys (
    key_hash TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL,
    scopes TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    last_used_at REAL,
    revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    principal TEXT NOT NULL,
    role TEXT NOT NULL,
    scopes TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_seen_at REAL,
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token_hash);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    agent TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'change_set',
    payload TEXT NOT NULL DEFAULT '{}',
    fingerprint TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    decided_by TEXT NOT NULL DEFAULT '',
    token_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    decided_at REAL
);
CREATE INDEX IF NOT EXISTS idx_approvals_task ON approvals(task_id);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);

CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    read_at REAL
);
CREATE INDEX IF NOT EXISTS idx_notifications_project
    ON notifications(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS server_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    """Thread-safe wrapper around one SQLite connection."""

    def __init__(self, path: "str | Path") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            # A restarting server may briefly overlap its predecessor;
            # wait instead of failing with "database is locked".
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO server_state (key, value) "
                "VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            self._conn.commit()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cursor

    def executemany(self, sql: str,
                    params: Sequence[Sequence[Any]]) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.executemany(
                sql, [tuple(row) for row in params])
            self._conn.commit()
            return cursor

    def query(self, sql: str,
              params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)))

    def query_one(self, sql: str,
                  params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def get_state(self, key: str, default: str = "") -> str:
        row = self.query_one(
            "SELECT value FROM server_state WHERE key = ?", (key,))
        return str(row["value"]) if row is not None else default

    def set_state(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO server_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)))

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


def row_to_dict(row: Any) -> "dict[str, Any]":
    """Convert a sqlite3.Row (or mapping) into a plain dict."""
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def json_list(value: Any) -> "List[Any]":
    """Parse a JSON list column, failing closed to an empty list."""
    import json

    if not value:
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        return []
    return list(parsed) if isinstance(parsed, list) else []


def json_dict(value: Any) -> "dict[str, Any]":
    """Parse a JSON object column, failing closed to an empty dict."""
    import json

    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def scope_tuple(rows: "Sequence[Tuple[str]]") -> "tuple[str, ...]":
    return tuple(str(row[0]) for row in rows)
