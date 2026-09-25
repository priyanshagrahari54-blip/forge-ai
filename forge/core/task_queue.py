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

    def __init__(self, engine: TaskEngine | None = None, store: TaskStore | None = None) -> None:
        self.engine = engine or TaskEngine()
        self.store = store or TaskStore()
        self._priorities: dict[str, int] = {}
        self._created_at: dict[str, str] = {}

    def add(self, task_id: str, description: str, dependencies: list[str] | None = None, priority: int = 0) -> Task:
        task = self.engine.add(task_id, description, dependencies=dependencies)
        self._priorities[task_id] = priority
        self._created_at[task_id] = datetime.now(timezone.utc).isoformat()
        self.store.save(task)
        return task

    def enqueue(self, task: Task, priority: int = 0) -> Task:
        if self.engine.exists(task.id):
            raise ValueError(f"Task already exists: {task.id}")
        self.engine.update(task)
        self._priorities[task.id] = priority
        self._created_at[task.id] = datetime.now(timezone.utc).isoformat()
        self.store.save(task)
        return task

    def save(self) -> None:
        self.store.save_all(self.engine.tasks)

    def load(self) -> list[Task]:
        tasks = self.store.load_all()
        self.engine.tasks = list(tasks)
        for task in tasks:
            self._priorities.setdefault(task.id, 0)
            self._created_at.setdefault(task.id, datetime.now(timezone.utc).isoformat())
        return tasks

    def ready(self) -> list[Task]:
        ready_tasks = [task for task in self.engine.tasks
                       if task.status == TaskStatus.PENDING and self.engine.can_start(task.id)]
        return sorted(ready_tasks, key=lambda task: (
            -self._priorities.get(task.id, 0), self._created_at.get(task.id, ""), task.id))

    def next(self) -> Task | None:
        # Bolt Optimization: Avoid calling self.ready() twice when fetching the next task.
        ready_tasks = self.ready()
        return ready_tasks[0] if ready_tasks else None

    def start_next(self) -> Task | None:
        for task in self.ready():
            claimed = self.store.claim(task.id)
            if claimed is None:
                continue
            self.engine.update(claimed)
            return claimed
        return None

    def renew_lease(self, task_id: str, lease_id: str) -> Task | None:
        """Refresh a worker lease and mirror the durable heartbeat locally."""
        task = self.store.renew_lease(task_id, lease_id)
        if task is None:
            return None
        self.engine.update(task)
        return task

    def recover_stale_running(self, max_idle_seconds: float) -> list[Task]:
        """Recover only leases that have exceeded the configured idle window."""
        tasks = self.store.recover_stale_running(max_idle_seconds)
        for task in tasks:
            self.engine.update(task)
        return tasks

    def complete(self, task_id: str) -> Task:
        task = self.engine.complete(task_id)
        self.store.save(task)
        return task

    def complete_if_owner(self, task_id: str, lease_id: str) -> Task | None:
        task = self.store.complete_if_owner(task_id, lease_id)
        if task is None:
            return None
        self.engine.update(task)
        return task

    def fail(self, task_id: str, error: str) -> Task:
        task = self.engine.fail(task_id, error)
        self.store.save(task)
        return task

    def fail_if_owner(self, task_id: str, lease_id: str, error: str) -> Task | None:
        task = self.store.fail_if_owner(task_id, lease_id, error)
        if task is None:
            return None
        self.engine.update(task)
        return task

    def pending(self) -> list[Task]:
        return [task for task in self.engine.tasks if task.status == TaskStatus.PENDING]

    def queued(self) -> list[QueuedTask]:
        return [QueuedTask(task=task, priority=self._priorities.get(task.id, 0),
                            created_at=self._created_at.get(task.id, ""))
                for task in self.engine.tasks]
