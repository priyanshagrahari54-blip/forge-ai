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
        # Performance optimization: Maintain a hash map index of tasks by ID
        # to ensure O(1) lookups during dependency checking, status transitions,
        # and validation, avoiding repeated linear scans over self.tasks.
        self._by_id: dict[str, Task] = {}
        self._last_tasks_id: int = id(self.tasks)
        self._last_tasks_len: int = 0

    def _sync(self) -> None:
        """Synchronize the internal hash index if self.tasks was mutated or reassigned."""
        current_id = id(self.tasks)
        current_len = len(self.tasks)
        if current_id != self._last_tasks_id or current_len != self._last_tasks_len:
            self._by_id = {task.id: task for task in self.tasks}
            self._last_tasks_id = current_id
            self._last_tasks_len = current_len

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
        self._last_tasks_len = len(self.tasks)
        self._last_tasks_id = id(self.tasks)
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
        self._sync()
        if task_id in self._by_id:
            return True
        # Fallback check in case an element within self.tasks was modified in-place
        # without changing the list length or reference.
        if any(task.id == task_id for task in self.tasks):
            self._by_id = {task.id: task for task in self.tasks}
            return True
        return False

    def _find(self, task_id: str) -> Task:
        self._sync()
        task = self._by_id.get(task_id)
        if task is not None:
            return task
        # Fallback check in case self.tasks was mutated in-place
        for t in self.tasks:
            if t.id == task_id:
                self._by_id = {task.id: task for task in self.tasks}
                return t
        raise KeyError(f"Task not found: {task_id}")
