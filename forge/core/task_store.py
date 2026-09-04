from __future__ import annotations

import sqlite3
from pathlib import Path

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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    errors TEXT NOT NULL DEFAULT '',
                    dependencies TEXT NOT NULL DEFAULT ''
                )
                """
            )

    def save(self, task: Task) -> None:
        errors = "\n".join(task.errors)
        dependencies = "\n".join(task.dependencies)

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    id,
                    description,
                    status,
                    attempts,
                    errors,
                    dependencies
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    description = excluded.description,
                    status = excluded.status,
                    attempts = excluded.attempts,
                    errors = excluded.errors,
                    dependencies = excluded.dependencies
                """,
                (
                    task.id,
                    task.description,
                    task.status.value,
                    task.attempts,
                    errors,
                    dependencies,
                ),
            )

    def save_all(self, tasks: list[Task]) -> None:
        for task in tasks:
            self.save(task)

    def load(self, task_id: str) -> Task:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()

        if row is None:
            raise KeyError(f"Task not found: {task_id}")

        return self._row_to_task(row)

    def load_all(self) -> list[Task]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM tasks
                ORDER BY rowid
                """
            ).fetchall()

        return [self._row_to_task(row) for row in rows]

    def delete(self, task_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM tasks WHERE id = ?",
                (task_id,),
            )

        if cursor.rowcount == 0:
            raise KeyError(f"Task not found: {task_id}")

    def clear(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM tasks")

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        errors = (
            row["errors"].splitlines()
            if row["errors"]
            else []
        )

        dependencies = (
            row["dependencies"].splitlines()
            if row["dependencies"]
            else []
        )

        return Task(
            id=row["id"],
            description=row["description"],
            status=TaskStatus(row["status"]),
            attempts=row["attempts"],
            errors=errors,
            dependencies=dependencies,
        )
