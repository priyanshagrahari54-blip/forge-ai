from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from forge.core.pipeline_agents import StageAgent, StageAgentResult
from forge.core.task_engine import Task, TaskStatus


@dataclass(frozen=True)
class PipelineStageResult:
    stage: TaskStatus
    success: bool
    output: str = ""
    error: str = ""
    agent: str = ""


class AgentPipeline:
    STAGES = (
        TaskStatus.PLANNING,
        TaskStatus.CODING,
        TaskStatus.TESTING,
        TaskStatus.REVIEWING,
    )

    def __init__(
        self,
        stage_handler: Callable[[Task, TaskStatus], str] | None = None,
        stage_agents: Mapping[TaskStatus, StageAgent] | None = None,
    ) -> None:
        if stage_handler is None and stage_agents is None:
            raise ValueError(
                "Either stage_handler or stage_agents must be provided"
            )

        if stage_handler is not None and stage_agents is not None:
            raise ValueError(
                "Provide stage_handler or stage_agents, not both"
            )

        self.stage_handler = stage_handler
        self.stage_agents = dict(stage_agents or {})

    def _execute_stage(
        self,
        task: Task,
        stage: TaskStatus,
    ) -> PipelineStageResult:
        if self.stage_agents:
            agent = self.stage_agents.get(stage)

            if agent is None:
                return PipelineStageResult(
                    stage=stage,
                    success=False,
                    error=f"No agent registered for stage: {stage.value}",
                )

            try:
                result: StageAgentResult = agent.execute(task)
            except Exception as exc:
                return PipelineStageResult(
                    stage=stage,
                    success=False,
                    error=str(exc),
                    agent=getattr(agent, "name", ""),
                )

            return PipelineStageResult(
                stage=stage,
                success=result.success,
                output=result.output,
                error=result.error,
                agent=result.agent,
            )

        if self.stage_handler is None:
            return PipelineStageResult(
                stage=stage,
                success=False,
                error=f"No handler registered for stage: {stage.value}",
            )

        try:
            output = self.stage_handler(task, stage)
        except Exception as exc:
            return PipelineStageResult(
                stage=stage,
                success=False,
                error=str(exc),
            )

        return PipelineStageResult(
            stage=stage,
            success=True,
            output=str(output),
        )

    def execute(self, task: Task) -> list[PipelineStageResult]:
        results: list[PipelineStageResult] = []

        for stage in self.STAGES:
            task.status = stage

            result = self._execute_stage(task, stage)
            results.append(result)

            if not result.success:
                task.status = TaskStatus.FAILED
                return results

        task.status = TaskStatus.COMPLETED
        return results
