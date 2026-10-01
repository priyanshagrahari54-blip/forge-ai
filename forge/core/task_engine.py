from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    RESEARCHING = "researching"
    CODING = "coding"
    TESTING = "testing"
    DEBUGGING = "debugging"
    REVIEWING = "reviewing"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RECOVERY = "recovery"


@dataclass
class Task:
    id: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    errors: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    lease_id: str = ""
    lease_heartbeat: float = 0.0


class TaskEngine:
    def __init__(self) -> None:
        self.tasks: list[Task] = []
        # O(1) index map for fast task lookups by task_id
        self._by_id: dict[str, Task] = {}

    def add(self, task_id: str, description: str, dependencies: list[str] | None = None) -> Task:
        if self._exists(task_id):
            raise ValueError(f"Task already exists: {task_id}")
        dependency_list = list(dependencies or [])
        for dependency in dependency_list:
            if dependency == task_id:
                raise ValueError(f"Task cannot depend on itself: {task_id}")
            if not self._exists(dependency):
                raise KeyError(f"Task dependency not found: {dependency}")
        task = Task(id=task_id, description=description, dependencies=dependency_list)
        self.tasks.append(task)
        self._by_id[task_id] = task
        return task

    def add_dependency(self, task_id: str, dependency_id: str) -> Task:
        task = self._find(task_id)
        if task_id == dependency_id:
            raise ValueError(f"Task cannot depend on itself: {task_id}")
        self._find(dependency_id)
        if dependency_id not in task.dependencies:
            task.dependencies.append(dependency_id)
        return task

    def can_start(self, task_id: str) -> bool:
        task = self._find(task_id)
        return all(self._find(dependency).status == TaskStatus.COMPLETED for dependency in task.dependencies)

    def start(self, task_id: str) -> Task:
        task = self._find(task_id)
        if not self.can_start(task_id):
            raise RuntimeError(f"Task dependencies are not completed: {task_id}")
        task.status = TaskStatus.RUNNING
        task.attempts += 1
        return task

    def complete(self, task_id: str) -> Task:
        task = self._find(task_id)
        task.status = TaskStatus.COMPLETED
        task.lease_id = ""
        task.lease_heartbeat = 0.0
        return task

    def fail(self, task_id: str, error: str) -> Task:
        task = self._find(task_id)
        task.status = TaskStatus.FAILED
        task.errors.append(error)
        task.lease_id = ""
        task.lease_heartbeat = 0.0
        return task

    def set_status(self, task_id: str, status: TaskStatus) -> Task:
        task = self._find(task_id)
        task.status = status
        if status != TaskStatus.RUNNING:
            task.lease_id = ""
            task.lease_heartbeat = 0.0
        return task

    def _exists(self, task_id: str) -> bool:
        # Fast O(1) check using dict index or fallback sync check if tasks modified directly
        return task_id in self._by_id or any(task.id == task_id for task in self.tasks)

    def _find(self, task_id: str) -> Task:
        # Fast O(1) lookup using dict index
        task = self._by_id.get(task_id)
        if task is not None:
            return task
        # Fallback scan and resync if self.tasks was mutated directly (e.g. self.engine.tasks = list(tasks))
        for t in self.tasks:
            if t.id == task_id:
                self._by_id[task_id] = t
                return t
        raise KeyError(f"Task not found: {task_id}")
