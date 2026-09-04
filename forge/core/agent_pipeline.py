from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from forge.core.pipeline_agents import StageAgent, StageAgentResult
from forge.core.task_engine import Task, TaskStatus
from forge.intelligence.agent_context import AgentContext


@dataclass(frozen=True)
class PipelineStageResult:
    stage: TaskStatus
    success: bool
    output: str = ""
    error: str = ""
    agent: str = ""
    context_fingerprint: str = ""


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
        context_provider: Callable[[Task], AgentContext] | None = None,
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
        self.context_provider = context_provider

    def _build_context(self, task: Task) -> AgentContext | None:
        if self.context_provider is None:
            return None
        return self.context_provider(task)

    def _execute_stage(
        self,
        task: Task,
        stage: TaskStatus,
        context: AgentContext | None = None,
    ) -> PipelineStageResult:
        if self.stage_agents:
            agent = self.stage_agents.get(stage)

            if agent is None:
                return PipelineStageResult(
                    stage=stage,
                    success=False,
                    error=f"No agent registered for stage: {stage.value}",
                    context_fingerprint=(
                        context.fingerprint if context else ""
                    ),
                )

            try:
                result: StageAgentResult = agent.execute(
                    task,
                    context,
                )
            except Exception as exc:
                return PipelineStageResult(
                    stage=stage,
                    success=False,
                    error=str(exc),
                    agent=getattr(agent, "name", ""),
                    context_fingerprint=(
                        context.fingerprint if context else ""
                    ),
                )

            return PipelineStageResult(
                stage=stage,
                success=result.success,
                output=result.output,
                error=result.error,
                agent=result.agent,
                context_fingerprint=result.context_fingerprint,
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

        context = self._build_context(task)

        for stage in self.STAGES:
            task.status = stage

            result = self._execute_stage(
                task,
                stage,
                context,
            )
            results.append(result)

            if not result.success:
                task.status = TaskStatus.FAILED
                return results

        task.status = TaskStatus.COMPLETED
        return results
