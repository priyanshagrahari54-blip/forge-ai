from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from forge.core.task_engine import Task, TaskStatus


class TaskStore:
    """SQLite-backed persistent storage for Forge tasks."""

    def __init__(self, path: str | Path = ".forge/tasks.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    errors TEXT NOT NULL DEFAULT '',
                    dependencies TEXT NOT NULL DEFAULT '',
                    lease_id TEXT NOT NULL DEFAULT '',
                    lease_heartbeat REAL NOT NULL DEFAULT 0
                )
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
            if "lease_id" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN lease_id TEXT NOT NULL DEFAULT ''")
            if "lease_heartbeat" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN lease_heartbeat REAL NOT NULL DEFAULT 0")

    def save(self, task: Task) -> None:
        errors = "\n".join(task.errors)
        dependencies = "\n".join(task.dependencies)
        heartbeat = getattr(task, "lease_heartbeat", 0.0)
        with self._connect() as connection:
            connection.execute("""
                INSERT INTO tasks (id, description, status, attempts, errors, dependencies, lease_id, lease_heartbeat)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    description = excluded.description,
                    status = excluded.status,
                    attempts = excluded.attempts,
                    errors = excluded.errors,
                    dependencies = excluded.dependencies,
                    lease_id = excluded.lease_id,
                    lease_heartbeat = excluded.lease_heartbeat
            """, (task.id, task.description, task.status.value, task.attempts,
                  errors, dependencies, task.lease_id, heartbeat))

    def save_all(self, tasks: list[Task]) -> None:
        for task in tasks:
            self.save(task)

    def load(self, task_id: str) -> Task:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Task not found: {task_id}")
        return self._row_to_task(row)

    def load_all(self) -> list[Task]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM tasks ORDER BY rowid").fetchall()
        return [self._row_to_task(row) for row in rows]

    def claim(self, task_id: str) -> Task | None:
        """Atomically claim a pending task and issue a unique worker lease."""
        lease_id = uuid4().hex
        heartbeat = time.time()
        with self._connect() as connection:
            cursor = connection.execute("""
                UPDATE tasks
                SET status = ?, attempts = attempts + 1, lease_id = ?, lease_heartbeat = ?
                WHERE id = ? AND status = ?
            """, (TaskStatus.RUNNING.value, lease_id, heartbeat,
                  task_id, TaskStatus.PENDING.value))
            if cursor.rowcount == 0:
                return None
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._row_to_task(row) if row is not None else None

    def renew_lease(self, task_id: str, lease_id: str) -> Task | None:
        """Refresh a lease only when the caller still owns the RUNNING task."""
        heartbeat = time.time()
        with self._connect() as connection:
            cursor = connection.execute("""
                UPDATE tasks SET lease_heartbeat = ?
                WHERE id = ? AND status = ? AND lease_id = ?
            """, (heartbeat, task_id, TaskStatus.RUNNING.value, lease_id))
            if cursor.rowcount == 0:
                return None
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._row_to_task(row) if row is not None else None

    def recover_stale_running(self, max_idle_seconds: float) -> list[Task]:
        """Atomically move only expired RUNNING leases into recovery."""
        if max_idle_seconds <= 0:
            raise ValueError("max_idle_seconds must be greater than zero")
        cutoff = time.time() - max_idle_seconds
        recovered: list[Task] = []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM tasks WHERE status = ? AND lease_heartbeat > 0 AND lease_heartbeat < ?",
                (TaskStatus.RUNNING.value, cutoff)).fetchall()
            for row in rows:
                cursor = connection.execute("""
                    UPDATE tasks
                    SET status = ?, lease_id = '', lease_heartbeat = 0,
                        errors = CASE WHEN errors = '' THEN ? ELSE errors || char(10) || ? END
                    WHERE id = ? AND status = ? AND lease_heartbeat < ?
                """, (TaskStatus.RECOVERY.value,
                      "Task lease expired and moved to recovery.",
                      "Task lease expired and moved to recovery.",
                      row["id"], TaskStatus.RUNNING.value, cutoff))
                if cursor.rowcount:
                    recovered_row = connection.execute(
                        "SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
                    if recovered_row is not None:
                        recovered.append(self._row_to_task(recovered_row))
        return recovered

    def complete_if_owner(self, task_id: str, lease_id: str) -> Task | None:
        """Complete only if this worker still owns the task lease."""
        with self._connect() as connection:
            cursor = connection.execute("""
                UPDATE tasks SET status = ?, lease_id = '', lease_heartbeat = 0
                WHERE id = ? AND status = ? AND lease_id = ?
            """, (TaskStatus.COMPLETED.value, task_id, TaskStatus.RUNNING.value, lease_id))
            if cursor.rowcount == 0:
                return None
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._row_to_task(row) if row is not None else None

    def fail_if_owner(self, task_id: str, lease_id: str, error: str) -> Task | None:
        """Fail only if this worker still owns the task lease."""
        with self._connect() as connection:
            row = connection.execute("SELECT errors FROM tasks WHERE id = ? AND status = ? AND lease_id = ?",
                                     (task_id, TaskStatus.RUNNING.value, lease_id)).fetchone()
            if row is None:
                return None
            errors = row["errors"]
            errors = f"{errors}\n{error}" if errors else error
            cursor = connection.execute("""
                UPDATE tasks SET status = ?, errors = ?, lease_id = '', lease_heartbeat = 0
                WHERE id = ? AND status = ? AND lease_id = ?
            """, (TaskStatus.FAILED.value, errors, task_id, TaskStatus.RUNNING.value, lease_id))
            if cursor.rowcount == 0:
                return None
            updated = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._row_to_task(updated) if updated is not None else None

    def recover_running(self, task_id: str) -> Task | None:
        """Atomically move one interrupted RUNNING task into RECOVERY."""
        with self._connect() as connection:
            cursor = connection.execute("""
                UPDATE tasks
                SET status = ?, lease_id = '', lease_heartbeat = 0,
                    errors = CASE WHEN errors = '' THEN ? ELSE errors || char(10) || ? END
                WHERE id = ? AND status = ?
            """, (TaskStatus.RECOVERY.value,
                  "Task interrupted and moved to recovery.",
                  "Task interrupted and moved to recovery.",
                  task_id, TaskStatus.RUNNING.value))
            if cursor.rowcount == 0:
                return None
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._row_to_task(row) if row is not None else None

    def delete(self, task_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cursor.rowcount == 0:
            raise KeyError(f"Task not found: {task_id}")

    def clear(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM tasks")

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            description=row["description"],
            status=TaskStatus(row["status"]),
            attempts=row["attempts"],
            errors=row["errors"].splitlines() if row["errors"] else [],
            dependencies=row["dependencies"].splitlines() if row["dependencies"] else [],
            lease_id=row["lease_id"] if "lease_id" in row.keys() else "",
        )
