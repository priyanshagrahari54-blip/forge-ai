"""Policy for long-running autonomous work without pretending compute is unlimited."""
from __future__ import annotations
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Callable


@dataclass(frozen=True)
class WorkBudget:
    """Logical budget: no fixed task-count ceiling, but every execution has safety bounds."""
    max_active_tasks: int = 4
    max_attempts_per_task: int = 8
    max_runtime_seconds: float = 24 * 60 * 60
    retry_backoff_seconds: float = 5.0
    allow_provider_rotation: bool = True
    allow_capability_discovery: bool = True


@dataclass
class WorkItem:
    task_id: str
    requirement: str
    priority: int = 0
    attempts: int = 0
    state: str = "queued"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=monotonic)


class AutonomousWorkloadController:
    """Keeps accepting work while enforcing resource, security and retry bounds.

    'Unlimited' means the queue is not capped by an artificial total task count.
    It does not mean unlimited provider credits, GPU, bandwidth, or wall-clock.
    """

    def __init__(self, *, budget: WorkBudget | None = None) -> None:
        self.budget = budget or WorkBudget()
        self._queue: list[WorkItem] = []
        self._active: dict[str, WorkItem] = {}
        self._completed = 0
        self._failed = 0

    def submit(self, item: WorkItem) -> WorkItem:
        if not item.task_id.strip() or not item.requirement.strip():
            raise ValueError("task_id and requirement are required")
        if item.task_id in self._active or any(x.task_id == item.task_id for x in self._queue):
            raise ValueError(f"task {item.task_id!r} is already queued or active")
        item.state = "queued"
        self._queue.append(item)
        self._queue.sort(key=lambda x: (-x.priority, x.created_at))
        return item

    def next(self) -> WorkItem | None:
        if len(self._active) >= self.budget.max_active_tasks or not self._queue:
            return None
        item = self._queue.pop(0)
        item.state = "running"
        self._active[item.task_id] = item
        return item

    def complete(self, task_id: str) -> None:
        item = self._active.pop(task_id, None)
        if item is None:
            raise KeyError(task_id)
        item.state = "completed"
        self._completed += 1

    def retry(self, task_id: str, *, reason: str = "") -> bool:
        item = self._active.pop(task_id, None)
        if item is None:
            raise KeyError(task_id)
        item.attempts += 1
        if item.attempts >= self.budget.max_attempts_per_task:
            item.state = "failed"
            item.metadata["failure_reason"] = reason or "retry budget exhausted"
            self._failed += 1
            return False
        item.state = "queued"
        item.metadata["last_failure"] = reason
        self._queue.append(item)
        self._queue.sort(key=lambda x: (-x.priority, x.created_at))
        return True

    def cancel(self, task_id: str) -> None:
        item = self._active.pop(task_id, None)
        if item is None:
            for index, queued in enumerate(self._queue):
                if queued.task_id == task_id:
                    item = self._queue.pop(index)
                    break
        if item is None:
            raise KeyError(task_id)
        item.state = "cancelled"

    def snapshot(self) -> dict[str, Any]:
        return {
            "queued": len(self._queue),
            "active": len(self._active),
            "completed": self._completed,
            "failed": self._failed,
            "budget": {
                "max_active_tasks": self.budget.max_active_tasks,
                "max_attempts_per_task": self.budget.max_attempts_per_task,
                "max_runtime_seconds": self.budget.max_runtime_seconds,
                "allow_provider_rotation": self.budget.allow_provider_rotation,
                "allow_capability_discovery": self.budget.allow_capability_discovery,
            },
        }
