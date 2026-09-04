from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from forge.core.task_engine import Task, TaskStatus


@dataclass(frozen=True)
class PipelineStageResult:
    stage: TaskStatus
    success: bool
    output: str = ""
    error: str = ""


class AgentPipeline:
    """Execute a task through ordered agent pipeline stages."""

    STAGES = (
        TaskStatus.PLANNING,
        TaskStatus.CODING,
        TaskStatus.TESTING,
        TaskStatus.REVIEWING,
    )

    def __init__(
        self,
        stage_handler: Callable[[Task, TaskStatus], str],
    ) -> None:
        self.stage_handler = stage_handler

    def execute(self, task: Task) -> list[PipelineStageResult]:
        results: list[PipelineStageResult] = []

        for stage in self.STAGES:
            task.status = stage

            try:
                output = self.stage_handler(task, stage)
            except Exception as exc:
                results.append(
                    PipelineStageResult(
                        stage=stage,
                        success=False,
                        error=str(exc),
                    )
                )
                task.status = TaskStatus.FAILED
                return results

            results.append(
                PipelineStageResult(
                    stage=stage,
                    success=True,
                    output=str(output),
                )
            )

        task.status = TaskStatus.COMPLETED
        return results
