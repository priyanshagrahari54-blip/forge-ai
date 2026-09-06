from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from forge.core.agent_executor import AgentExecutor
from forge.core.agent_pipeline import AgentPipeline, PipelineStageResult
from forge.core.task_engine import Task, TaskStatus
from forge.core.task_queue import PersistentTaskQueue
from forge.core.task_recovery import TaskRecoveryEngine
from forge.performance.metrics import MetricsRecorder


@dataclass(frozen=True)
class TaskExecutionResult:
    task_id: str
    success: bool
    output: str = ""
    error: str = ""
    agent: str = ""
    stages: tuple[PipelineStageResult, ...] = ()


class TaskExecutionCoordinator:
    """Coordinate tasks, agents, pipelines, persistence, and recovery."""

    def __init__(
        self,
        queue: PersistentTaskQueue,
        recovery: TaskRecoveryEngine,
        metrics: MetricsRecorder | None = None,
        timer: Callable[[], float] | None = None,
    ) -> None:
        self.queue = queue
        self.recovery = recovery
        self.metrics = metrics
        self.timer = timer or time.perf_counter

    def _record_task_metric(
        self,
        task: Task,
        *,
        agent: str,
        status: str,
        duration_ms: float,
    ) -> None:
        if self.metrics is None:
            return

        self.metrics.record(
            task_id=task.id,
            stage="task",
            agent=agent,
            status=status,
            duration_ms=duration_ms,
            attempts=task.attempts,
            retries=max(0, task.attempts - 1),
        )

    def recover(self) -> list[Task]:
        return self.recovery.recover()

    def next_task(self) -> Task | None:
        return self.queue.next()

    def execute(
        self,
        task_id: str,
        agent: AgentExecutor,
    ) -> TaskExecutionResult:
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

        timer_started = self.timer()
        result = agent.execute(started)
        duration_ms = (self.timer() - timer_started) * 1000

        if result.success:
            self.queue.complete(task_id)
        else:
            self.queue.fail(task_id, result.error)

        self._record_task_metric(
            started,
            agent=getattr(agent, "agent_name", ""),
            status=(
                "success" if result.success else "failure"
            ),
            duration_ms=duration_ms,
        )

        return TaskExecutionResult(
            task_id=task_id,
            success=result.success,
            output=result.output,
            error=result.error,
            agent=result.agent,
        )

    def execute_pipeline(
        self,
        task_id: str,
        pipeline: AgentPipeline,
    ) -> TaskExecutionResult:
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

        if pipeline.metrics_recorder is None and self.metrics is not None:
            pipeline.metrics_recorder = self.metrics

        timer_started = self.timer()
        results = pipeline.execute(started)
        duration_ms = (self.timer() - timer_started) * 1000
        successful = all(result.success for result in results)

        if successful:
            self.queue.complete(task_id)
        else:
            failed = next(
                result for result in results if not result.success
            )
            self.queue.fail(task_id, failed.error)

        self._record_task_metric(
            started,
            agent="pipeline",
            status=(
                "success" if successful else "failure"
            ),
            duration_ms=duration_ms,
        )

        output = "\n".join(
            result.output
            for result in results
            if result.output
        )

        error = next(
            (result.error for result in results if result.error),
            "",
        )

        return TaskExecutionResult(
            task_id=task_id,
            success=successful,
            output=output,
            error=error,
            stages=tuple(results),
        )

    def run_next(
        self,
        agent: AgentExecutor,
    ) -> TaskExecutionResult | None:
        task = self.next_task()

        if task is None:
            return None

        return self.execute(task.id, agent)

    def run_pipeline_next(
        self,
        pipeline: AgentPipeline,
    ) -> TaskExecutionResult | None:
        task = self.next_task()

        if task is None:
            return None

        return self.execute_pipeline(task.id, pipeline)

    def run_until_idle(
        self,
        agent: AgentExecutor,
        max_tasks: int = 100,
    ) -> list[TaskExecutionResult]:
        if max_tasks < 1:
            raise ValueError("max_tasks must be at least 1")

        results: list[TaskExecutionResult] = []

        for _ in range(max_tasks):
            result = self.run_next(agent)

            if result is None:
                break

            results.append(result)

            if not result.success:
                break

        return results

    def run_pipeline_until_idle(
        self,
        pipeline: AgentPipeline,
        max_tasks: int = 100,
    ) -> list[TaskExecutionResult]:
        if max_tasks < 1:
            raise ValueError("max_tasks must be at least 1")

        results: list[TaskExecutionResult] = []

        for _ in range(max_tasks):
            result = self.run_pipeline_next(pipeline)

            if result is None:
                break

            results.append(result)

            if not result.success:
                break

        return results
