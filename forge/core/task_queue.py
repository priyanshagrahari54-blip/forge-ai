from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from forge.core.task_engine import Task, TaskEngine, TaskStatus
from forge.core.task_store import TaskStore


@dataclass(frozen=True)
class QueuedTask:
    task: Task
    priority: int = 0
    created_at: str = ""


class PersistentTaskQueue:
    """Persistent priority queue backed by TaskStore."""

    def __init__(
        self,
        engine: TaskEngine | None = None,
        store: TaskStore | None = None,
    ) -> None:
        self.engine = engine or TaskEngine()
        self.store = store or TaskStore()
        self._priorities: dict[str, int] = {}
        self._created_at: dict[str, str] = {}

    def add(
        self,
        task_id: str,
        description: str,
        dependencies: list[str] | None = None,
        priority: int = 0,
    ) -> Task:
        task = self.engine.add(
            task_id,
            description,
            dependencies=dependencies,
        )

        self._priorities[task_id] = priority
        self._created_at[task_id] = datetime.now(
            timezone.utc
        ).isoformat()

        self.store.save(task)
        return task

    def enqueue(self, task: Task, priority: int = 0) -> Task:
        if any(existing.id == task.id for existing in self.engine.tasks):
            raise ValueError(f"Task already exists: {task.id}")

        self.engine.tasks.append(task)
        self._priorities[task.id] = priority
        self._created_at[task.id] = datetime.now(
            timezone.utc
        ).isoformat()

        self.store.save(task)
        return task

    def save(self) -> None:
        self.store.save_all(self.engine.tasks)

    def load(self) -> list[Task]:
        tasks = self.store.load_all()

        self.engine.tasks = list(tasks)

        for task in tasks:
            self._priorities.setdefault(task.id, 0)
            self._created_at.setdefault(
                task.id,
                datetime.now(timezone.utc).isoformat(),
            )

        return tasks

    def ready(self) -> list[Task]:
        """Return pending tasks whose dependencies are completed."""
        ready_tasks = [
            task
            for task in self.engine.tasks
            if task.status == TaskStatus.PENDING
            and self.engine.can_start(task.id)
        ]

        return sorted(
            ready_tasks,
            key=lambda task: (
                -self._priorities.get(task.id, 0),
                self._created_at.get(task.id, ""),
                task.id,
            ),
        )

    def next(self) -> Task | None:
        """Return the highest-priority ready task."""
        tasks = self.ready()
        return tasks[0] if tasks else None

    def start_next(self) -> Task | None:
        """Start and persist the highest-priority ready task."""
        task = self.next()

        if task is None:
            return None

        started = self.engine.start(task.id)
        self.store.save(started)
        return started

    def complete(self, task_id: str) -> Task:
        task = self.engine.complete(task_id)
        self.store.save(task)
        return task

    def fail(self, task_id: str, error: str) -> Task:
        task = self.engine.fail(task_id, error)
        self.store.save(task)
        return task

    def pending(self) -> list[Task]:
        return [
            task
            for task in self.engine.tasks
            if task.status == TaskStatus.PENDING
        ]

    def queued(self) -> list[QueuedTask]:
        return [
            QueuedTask(
                task=task,
                priority=self._priorities.get(task.id, 0),
                created_at=self._created_at.get(task.id, ""),
            )
            for task in self.engine.tasks
        ]
