"""Persistent execution state (A81).

:class:`ExecutionStateStore` is a SQLite-backed journal of one
orchestration system's runs: the run record, every task's state, every
attempt, every structured message, and every scheduler event.

Properties:

* **durable** — every transition is committed as it happens, so a crash
  mid-run never loses what already finished;
* **resumable** — :meth:`load_graph` rebuilds a :class:`TaskGraph` with
  terminal states preserved and every RUNNING task re-admitted as
  PENDING, so a restarted scheduler continues exactly where it stopped;
* **inspectable** — the desktop views and API read runs, tasks,
  messages, and events straight from this store.

The store is single-process (one scheduler per store file); connections
are short-lived and serialized, matching the rest of Forge's SQLite use.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from forge.orchestration.graph import TaskGraph

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    project TEXT NOT NULL DEFAULT '',
    requirement TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'RUNNING',
    config TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    summary TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tasks (
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    spec TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    blocked_reason TEXT NOT NULL DEFAULT '',
    started_at REAL,
    finished_at REAL,
    PRIMARY KEY (run_id, task_id)
);
CREATE TABLE IF NOT EXISTS attempts (
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    finished_at REAL NOT NULL,
    PRIMARY KEY (run_id, task_id, attempt)
);
CREATE TABLE IF NOT EXISTS messages (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    at REAL NOT NULL,
    name TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""


class ExecutionStateStore:
    """SQLite journal of runs, tasks, attempts, messages, and events."""

    def __init__(self, path: str | Path = ".forge/executions.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        return connection

    # -- runs ---------------------------------------------------------------

    def create_run(self, run_id: str, project: str = "",
                   requirement: str = "",
                   config: dict[str, Any] | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO runs (run_id, project, requirement, status,"
                " config, created_at, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(run_id) DO NOTHING",
                (run_id, project, requirement, "RUNNING",
                 json.dumps(config or {}, default=str), time.time(),
                 time.time()))

    def update_run(self, run_id: str, status: str, summary: str = "",
                   finished_at: float | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET status = ?, summary = ?, finished_at = ?"
                " WHERE run_id = ?",
                (status, summary,
                 finished_at if finished_at is not None else time.time(),
                 run_id))

    # -- tasks / attempts ------------------------------------------------------

    def save_task(self, run_id: str, task: Any) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO tasks (run_id, task_id, spec, status, attempts,"
                " result, error, blocked_reason, started_at, finished_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(run_id, task_id) DO UPDATE SET"
                " spec = excluded.spec,"
                " status = excluded.status,"
                " attempts = excluded.attempts,"
                " result = excluded.result,"
                " error = excluded.error,"
                " blocked_reason = excluded.blocked_reason,"
                " started_at = excluded.started_at,"
                " finished_at = excluded.finished_at",
                (run_id, task.id, json.dumps(task.to_dict(), default=str),
                 task.status.value, task.attempts,
                 json.dumps(task.result, default=str) if task.result is not None
                 else "",
                 task.error, task.blocked_reason, task.started_at,
                 task.finished_at))

    def record_attempt(self, run_id: str, task_id: str, attempt: int,
                       outcome: str, error: str = "",
                       started_at: float | None = None,
                       finished_at: float | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO attempts (run_id, task_id, attempt, outcome,"
                " error, started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(run_id, task_id, attempt) DO UPDATE SET"
                " outcome = excluded.outcome, error = excluded.error,"
                " finished_at = excluded.finished_at",
                (run_id, task_id, attempt, outcome, error,
                 started_at if started_at is not None else time.time(),
                 finished_at if finished_at is not None else time.time()))

    # -- messages / events --------------------------------------------------------

    def record_message(self, run_id: str, payload: dict[str, Any]) -> None:
        seq = self._next_seq(connection=None, table="messages",
                             run_id=run_id)
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO messages (run_id, seq, payload)"
                " VALUES (?, ?, ?)",
                (run_id, seq, json.dumps(payload, default=str)))

    def record_event(self, run_id: str, name: str,
                     payload: dict[str, Any]) -> None:
        seq = self._next_seq(table="events", run_id=run_id)
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO events (run_id, seq, at, name, payload)"
                " VALUES (?, ?, ?, ?, ?)",
                (run_id, seq, time.time(), name,
                 json.dumps(payload, default=str)))

    def _next_seq(self, table: str, run_id: str,
                  connection: sqlite3.Connection | None = None) -> int:
        if connection is not None:
            row = connection.execute(
                f"SELECT COALESCE(MAX(seq), -1) AS m FROM {table}"
                " WHERE run_id = ?", (run_id,)).fetchone()
            return int(row["m"]) + 1
        with self._connect() as own:
            row = own.execute(
                f"SELECT COALESCE(MAX(seq), -1) AS m FROM {table}"
                " WHERE run_id = ?", (run_id,)).fetchone()
            return int(row["m"]) + 1

    # -- loading ------------------------------------------------------------------

    def load_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown run: {run_id!r}")
        run = {
            "run_id": row["run_id"], "project": row["project"],
            "requirement": row["requirement"], "status": row["status"],
            "config": json.loads(row["config"] or "{}"),
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "summary": row["summary"],
        }
        with self._connect() as connection:
            task_rows = connection.execute(
                "SELECT * FROM tasks WHERE run_id = ? ORDER BY rowid",
                (run_id,)).fetchall()
            attempt_rows = connection.execute(
                "SELECT * FROM attempts WHERE run_id = ?"
                " ORDER BY task_id, attempt", (run_id,)).fetchall()
            message_rows = connection.execute(
                "SELECT payload FROM messages WHERE run_id = ?"
                " ORDER BY seq", (run_id,)).fetchall()
            event_rows = connection.execute(
                "SELECT at, name, payload FROM events WHERE run_id = ?"
                " ORDER BY seq", (run_id,)).fetchall()
        return {
            "run": run,
            "tasks": [json.loads(task_row["spec"]) for task_row in task_rows],
            "attempts": [dict(attempt_row) for attempt_row in attempt_rows],
            "messages": [json.loads(row["payload"])
                         for row in message_rows],
            "events": [
                {"at": row["at"], "name": row["name"],
                 "payload": json.loads(row["payload"])}
                for row in event_rows
            ],
        }

    def load_graph(self, run_id: str) -> TaskGraph:
        """Rebuild the graph for a resumed run.

        SUCCEEDED tasks keep their results (they never re-run); every
        other state (RUNNING, FAILED, DENIED, BLOCKED, CANCELLED) is
        re-admitted as PENDING so a restart continues — not repeats —
        the work. Attempts counts are preserved for the record.
        """
        loaded = self.load_run(run_id)
        data = {
            "name": f"resumed-{run_id[:8]}",
            "tasks": loaded["tasks"],
        }
        for task in data["tasks"]:
            if task.get("status") != "SUCCEEDED":
                task["status"] = "PENDING"
                task["started_at"] = None
                task["result"] = None
                task["error"] = ""
                task["blocked_reason"] = ""
        graph = TaskGraph.from_dict(data)
        return graph

    def list_runs(self, project: str = "") -> list[dict[str, Any]]:
        with self._connect() as connection:
            if project:
                rows = connection.execute(
                    "SELECT * FROM runs WHERE project = ?"
                    " ORDER BY created_at DESC", (project,)).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM runs ORDER BY created_at DESC"
                ).fetchall()
        return [
            {
                "run_id": row["run_id"], "project": row["project"],
                "requirement": row["requirement"],
                "status": row["status"],
                "created_at": row["created_at"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "summary": row["summary"],
            }
            for row in rows
        ]
