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
    """Task engine managing tasks and dependency checks.

    Bolt Optimization: Uses internal `_by_id` and `_index` dictionaries for O(1)
    task lookups and updates during dependency verification and state transitions.
    Replaces O(N) linear scans with O(1) operations during scheduling passes.
    """

    def __init__(self) -> None:
        self._tasks: list[Task] = []
        self._by_id: dict[str, Task] = {}
        self._index: dict[str, int] = {}

    @property
    def tasks(self) -> list[Task]:
        return self._tasks

    @tasks.setter
    def tasks(self, value: list[Task]) -> None:
        self._tasks = list(value)
        self._by_id = {task.id: task for task in self._tasks}
        self._index = {task.id: i for i, task in enumerate(self._tasks)}

    def update(self, task: Task) -> None:
        """Add or replace a Task instance in the engine with O(1) index updating."""
        idx = self._index.get(task.id)
        if idx is not None and idx < len(self._tasks) and self._tasks[idx].id == task.id:
            self._tasks[idx] = task
            self._by_id[task.id] = task
        else:
            self._index[task.id] = len(self._tasks)
            self._tasks.append(task)
            self._by_id[task.id] = task

    def add(self, task_id: str, description: str, dependencies: list[str] | None = None) -> Task:
        if self.exists(task_id):
            raise ValueError(f"Task already exists: {task_id}")
        dependency_list = list(dependencies or [])
        for dependency in dependency_list:
            if dependency == task_id:
                raise ValueError(f"Task cannot depend on itself: {task_id}")
            if not self.exists(dependency):
                raise KeyError(f"Task dependency not found: {dependency}")
        task = Task(id=task_id, description=description, dependencies=dependency_list)
        self.update(task)
        return task

    def add_dependency(self, task_id: str, dependency_id: str) -> Task:
        task = self.find(task_id)
        if task_id == dependency_id:
            raise ValueError(f"Task cannot depend on itself: {task_id}")
        self.find(dependency_id)
        if dependency_id not in task.dependencies:
            task.dependencies.append(dependency_id)
        return task

    def can_start(self, task_id: str) -> bool:
        task = self.find(task_id)
        return all(self.find(dependency).status == TaskStatus.COMPLETED for dependency in task.dependencies)

    def start(self, task_id: str) -> Task:
        task = self.find(task_id)
        if not self.can_start(task_id):
            raise RuntimeError(f"Task dependencies are not completed: {task_id}")
        task.status = TaskStatus.RUNNING
        task.attempts += 1
        return task

    def complete(self, task_id: str) -> Task:
        task = self.find(task_id)
        task.status = TaskStatus.COMPLETED
        task.lease_id = ""
        task.lease_heartbeat = 0.0
        return task

    def fail(self, task_id: str, error: str) -> Task:
        task = self.find(task_id)
        task.status = TaskStatus.FAILED
        task.errors.append(error)
        task.lease_id = ""
        task.lease_heartbeat = 0.0
        return task

    def set_status(self, task_id: str, status: TaskStatus) -> Task:
        task = self.find(task_id)
        task.status = status
        if status != TaskStatus.RUNNING:
            task.lease_id = ""
            task.lease_heartbeat = 0.0
        return task

    def exists(self, task_id: str) -> bool:
        return task_id in self._by_id

    def find(self, task_id: str) -> Task:
        task = self._by_id.get(task_id)
        if task is not None:
            return task
        raise KeyError(f"Task not found: {task_id}")

    # Aliases for backward compatibility
    _exists = exists
    _find = find
