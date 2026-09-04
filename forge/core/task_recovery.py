from __future__ import annotations

from dataclasses import dataclass

from forge.core.task_engine import Task, TaskStatus
from forge.core.task_store import TaskStore


@dataclass(frozen=True)
class RecoveryPolicy:
    max_attempts: int = 3
    retry_failed: bool = True


class TaskRecoveryEngine:
    """Recover interrupted tasks and decide which tasks can be retried."""

    def __init__(
        self,
        store: TaskStore,
        policy: RecoveryPolicy | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or RecoveryPolicy()

    def recover_interrupted(self) -> list[Task]:
        """Move RUNNING tasks to RECOVERY after a restart."""
        tasks = self.store.load_all()
        recovered: list[Task] = []

        for task in tasks:
            if task.status == TaskStatus.RUNNING:
                task.status = TaskStatus.RECOVERY
                task.errors.append("Task interrupted and moved to recovery.")
                self.store.save(task)
                recovered.append(task)

        return recovered

    def recover_failed(self) -> list[Task]:
        """Move retryable FAILED tasks back to PENDING."""
        if not self.policy.retry_failed:
            return []

        tasks = self.store.load_all()
        recovered: list[Task] = []

        for task in tasks:
            if (
                task.status == TaskStatus.FAILED
                and task.attempts < self.policy.max_attempts
            ):
                task.status = TaskStatus.PENDING
                task.errors.append("Task scheduled for retry.")
                self.store.save(task)
                recovered.append(task)

        return recovered

    def recover(self) -> list[Task]:
        """Perform the complete restart recovery pass."""
        recovered = self.recover_interrupted()

        recovered_ids = {task.id for task in recovered}

        for task in self.recover_failed():
            if task.id not in recovered_ids:
                recovered.append(task)

        return recovered

    def retryable(self, task: Task) -> bool:
        """Return whether a task is allowed another attempt."""
        if task.attempts >= self.policy.max_attempts:
            return False

        if task.status == TaskStatus.FAILED:
            return self.policy.retry_failed

        return task.status in {
            TaskStatus.RECOVERY,
            TaskStatus.PENDING,
        }

    def mark_recovery(self, task: Task, reason: str) -> Task:
        """Explicitly place a task into recovery."""
        task.status = TaskStatus.RECOVERY
        task.errors.append(reason)
        self.store.save(task)
        return task

    def mark_retry(self, task: Task) -> Task:
        """Return a recoverable task to the pending queue."""
        if not self.retryable(task):
            raise RuntimeError(
                f"Task cannot be retried: {task.id}"
            )

        task.status = TaskStatus.PENDING
        self.store.save(task)
        return task
