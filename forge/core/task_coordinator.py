from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from forge.core.task_engine import Task, TaskStatus
from forge.core.task_queue import PersistentTaskQueue
from forge.core.task_recovery import TaskRecoveryEngine


@dataclass(frozen=True)
class TaskExecutionResult:
    task_id: str
    success: bool
    output: str = ""
    error: str = ""


class TaskExecutionCoordinator:
    """Coordinate task selection, execution, persistence, and recovery."""

    def __init__(
        self,
        queue: PersistentTaskQueue,
        recovery: TaskRecoveryEngine,
    ) -> None:
        self.queue = queue
        self.recovery = recovery

    def recover(self) -> list[Task]:
        """Recover interrupted/retryable tasks before execution."""
        return self.recovery.recover()

    def next_task(self) -> Task | None:
        """Return the next dependency-ready task."""
        return self.queue.next()

    def execute(
        self,
        task_id: str,
        worker: Callable[[Task], str],
    ) -> TaskExecutionResult:
        """Execute one task through a supplied worker function."""
        task = self.queue.engine._find(task_id)

        if task.status != TaskStatus.PENDING:
            return TaskExecutionResult(
                task_id=task_id,
                success=False,
                error=f"Task is not pending: {task_id}",
            )

        if not self.queue.engine.can_start(task_id):
            return TaskExecutionResult(
                task_id=task_id,
                success=False,
                error=f"Task dependencies are not completed: {task_id}",
            )

        started = self.queue.engine.start(task_id)
        self.queue.store.save(started)

        try:
            output = worker(started)
        except Exception as exc:
            self.queue.fail(task_id, str(exc))
            return TaskExecutionResult(
                task_id=task_id,
                success=False,
                error=str(exc),
            )

        self.queue.complete(task_id)

        return TaskExecutionResult(
            task_id=task_id,
            success=True,
            output=output,
        )

    def run_next(
        self,
        worker: Callable[[Task], str],
    ) -> TaskExecutionResult | None:
        """Execute the next ready task."""
        task = self.next_task()

        if task is None:
            return None

        return self.execute(task.id, worker)

    def run_until_idle(
        self,
        worker: Callable[[Task], str],
        max_tasks: int = 100,
    ) -> list[TaskExecutionResult]:
        """Execute ready tasks until no task remains or the limit is reached."""
        if max_tasks < 1:
            raise ValueError("max_tasks must be at least 1")

        results: list[TaskExecutionResult] = []

        for _ in range(max_tasks):
            result = self.run_next(worker)

            if result is None:
                break

            results.append(result)

            if not result.success:
                break

        return results
