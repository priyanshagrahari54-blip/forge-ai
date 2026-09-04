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


class TaskEngine:
    def __init__(self) -> None:
        self.tasks: list[Task] = []

    def add(self, task_id: str, description: str) -> Task:
        task = Task(task_id, description)
        self.tasks.append(task)
        return task

    def start(self, task_id: str) -> Task:
        task = self._find(task_id)
        task.status = TaskStatus.RUNNING
        task.attempts += 1
        return task

    def complete(self, task_id: str) -> Task:
        task = self._find(task_id)
        task.status = TaskStatus.COMPLETED
        return task

    def set_status(self, task_id: str, status: TaskStatus) -> Task:
        task = self._find(task_id)
        task.status = status
        return task

    def fail(self, task_id: str, error: str) -> Task:
        task = self._find(task_id)
        task.status = TaskStatus.FAILED
        task.errors.append(error)
        return task

    def _find(self, task_id: str) -> Task:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise KeyError(f"Task not found: {task_id}")
