from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from forge.core.planner import Planner
from forge.core.state import ForgeState
from forge.core.task_engine import Task, TaskEngine, TaskStatus


@dataclass(frozen=True)
class SupervisorDecision:
    """A decision made by the supervisor about task execution."""

    action: str  # "execute", "skip", "retry", "fail", "wait"
    task_id: str
    reason: str = ""
    agent: str = ""


@dataclass
class SupervisorStats:
    """Statistics for a supervisor orchestration run."""

    total: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    retried: int = 0


class Supervisor:
    """Multi-task orchestration with dependency resolution and retry logic.

    The supervisor manages a queue of tasks, respects their dependencies,
    and coordinates execution through a pluggable executor callback.
    """

    def __init__(
        self,
        project_name: str,
        engine: TaskEngine | None = None,
        max_retries: int = 3,
    ) -> None:
        self.state = ForgeState(project_name)
        self.planner = Planner()
        self.engine = engine or TaskEngine()
        self.max_retries = max_retries
        self.stats = SupervisorStats()

    def create_plan(self, request: str):
        return self.planner.create_plan(request)

    def start(self, task: str) -> None:
        self.state.start_task(task)

    def complete(self) -> None:
        self.state.complete_task()

    def add_task(
        self,
        task_id: str,
        description: str,
        dependencies: list[str] | None = None,
    ) -> Task:
        """Add a task to the supervisor's engine."""
        return self.engine.add(task_id, description, dependencies)

    def get_ready_tasks(self) -> list[Task]:
        """Return tasks whose dependencies are all completed."""
        return [
            task
            for task in self.engine.tasks
            if task.status == TaskStatus.PENDING
            and self.engine.can_start(task.id)
        ]

    def get_blocked_tasks(self) -> list[Task]:
        """Return tasks that are waiting on dependencies."""
        return [
            task
            for task in self.engine.tasks
            if task.status == TaskStatus.PENDING
            and not self.engine.can_start(task.id)
        ]

    def get_failed_tasks(self) -> list[Task]:
        """Return tasks that have failed."""
        return [
            task
            for task in self.engine.tasks
            if task.status == TaskStatus.FAILED
        ]

    def decide(self, task: Task) -> SupervisorDecision:
        """Decide what to do with a task based on its state."""
        if task.status == TaskStatus.COMPLETED:
            return SupervisorDecision(
                action="skip",
                task_id=task.id,
                reason="Task already completed",
            )

        if task.status == TaskStatus.FAILED:
            if task.attempts < self.max_retries:
                return SupervisorDecision(
                    action="retry",
                    task_id=task.id,
                    reason=f"Retrying failed task (attempt {task.attempts + 1}/{self.max_retries})",
                )
            return SupervisorDecision(
                action="fail",
                task_id=task.id,
                reason=f"Task failed after {task.attempts} attempts",
            )

        if task.status == TaskStatus.RUNNING:
            return SupervisorDecision(
                action="wait",
                task_id=task.id,
                reason="Task is already running",
            )

        if not self.engine.can_start(task.id):
            return SupervisorDecision(
                action="wait",
                task_id=task.id,
                reason="Dependencies not yet completed",
            )

        return SupervisorDecision(
            action="execute",
            task_id=task.id,
            reason="Task is ready for execution",
        )

    def run(
        self,
        executor: Callable[[Task], bool],
        max_cycles: int = 100,
    ) -> SupervisorStats:
        """Run the supervisor until all tasks are done or max_cycles reached.

        The executor callback takes a Task and returns True on success.
        """
        if max_cycles < 1:
            raise ValueError("max_cycles must be at least 1")

        self.stats = SupervisorStats(total=len(self.engine.tasks))

        for _ in range(max_cycles):
            if self._all_settled():
                break

            ready = self.get_ready_tasks()

            if not ready:
                blocked = self.get_blocked_tasks()
                if blocked and not self._has_running():
                    for task in blocked:
                        self.stats.failed += 1
                        self.engine.fail(
                            task.id,
                            "Deadlock: dependencies cannot be satisfied",
                        )
                break

            for task in ready:
                decision = self.decide(task)

                if decision.action == "execute":
                    self.engine.start(task.id)
                    success = executor(task)
                    if success:
                        self.engine.complete(task.id)
                        self.stats.completed += 1
                    else:
                        self.engine.fail(task.id, "Executor returned failure")
                        if task.attempts < self.max_retries:
                            self.engine.set_status(task.id, TaskStatus.PENDING)
                            self.stats.retried += 1
                        else:
                            self.stats.failed += 1
                elif decision.action == "retry":
                    self.engine.start(task.id)
                    success = executor(task)
                    if success:
                        self.engine.complete(task.id)
                        self.stats.completed += 1
                        self.stats.retried += 1
                    else:
                        if task.attempts < self.max_retries:
                            self.engine.set_status(task.id, TaskStatus.PENDING)
                        else:
                            self.stats.failed += 1

        return self.stats

    def _all_settled(self) -> bool:
        """Return True if all tasks are completed or permanently failed."""
        return all(
            task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
            for task in self.engine.tasks
        ) and not self._has_running()

    def _has_running(self) -> bool:
        """Return True if any task is currently running."""
        return any(
            task.status == TaskStatus.RUNNING
            for task in self.engine.tasks
        )

    def reset(self) -> None:
        """Reset all tasks to pending state."""
        for task in self.engine.tasks:
            task.status = TaskStatus.PENDING
            task.attempts = 0
            task.errors.clear()
        self.stats = SupervisorStats(total=len(self.engine.tasks))
