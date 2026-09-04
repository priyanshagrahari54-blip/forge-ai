from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from forge.agents.registry import AgentRegistry
from forge.agents.planner import AgentPlan
from forge.agents.stage_executor import ExecutorStageAgent
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

    STAGE_ROLES = {
        TaskStatus.PLANNING: "planning",
        TaskStatus.CODING: "coding",
        TaskStatus.TESTING: "testing",
        TaskStatus.REVIEWING: "reviewing",
    }

    CAPABILITY_STAGES = {
        "planning": TaskStatus.PLANNING,
        "coding": TaskStatus.CODING,
        "testing": TaskStatus.TESTING,
        "debugging": TaskStatus.DEBUGGING,
        "review": TaskStatus.REVIEWING,
        "security": TaskStatus.REVIEWING,
        "documentation": TaskStatus.RUNNING,
    }

    def __init__(
        self,
        stage_handler: Callable[[Task, TaskStatus], str] | None = None,
        stage_agents: Mapping[TaskStatus, StageAgent] | None = None,
        context_provider: Callable[[Task], AgentContext] | None = None,
        agent_registry: AgentRegistry | None = None,
    ) -> None:
        if stage_handler is None and stage_agents is None and agent_registry is None:
            raise ValueError(
                "Either stage_handler, stage_agents, or agent_registry "
                "must be provided"
            )

        if stage_handler is not None and (
            stage_agents is not None or agent_registry is not None
        ):
            raise ValueError(
                "Provide stage_handler or stage_agents/agent_registry, "
                "not both"
            )

        if stage_agents is not None and agent_registry is not None:
            raise ValueError(
                "Provide stage_agents or agent_registry, not both"
            )

        self.stage_handler = stage_handler
        self.stage_agents = dict(stage_agents or {})
        self.agent_registry = agent_registry
        self.context_provider = context_provider

    def _resolve_agent(self, stage: TaskStatus) -> StageAgent | None:
        if self.agent_registry is None:
            return self.stage_agents.get(stage)

        role = self.STAGE_ROLES[stage]
        registrations = self.agent_registry.get_by_role(role)

        if not registrations:
            return None

        registration = registrations[0]
        return ExecutorStageAgent(
            registration.executor,
            stage,
        )

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
        agent = self._resolve_agent(stage)

        if agent is not None:
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

        if self.stage_handler is not None:
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

        return PipelineStageResult(
            stage=stage,
            success=False,
            error=(
                f"No agent registered for stage: {stage.value}"
            ),
        )

    def execute_plan(
        self,
        task: Task,
        plan: AgentPlan,
    ) -> list[PipelineStageResult]:
        """Execute a precomputed multi-agent plan.

        The plan is executed in its deterministic order. Each planned
        agent is adapted through the existing ExecutorStageAgent layer,
        so executor responses, context fingerprints, errors, and agent
        identity retain the existing pipeline semantics.

        The normal execute() method remains unchanged for the standard
        four-stage pipeline.
        """
        results: list[PipelineStageResult] = []

        context = self._build_context(task)

        for planned in plan.agents:
            stage = self.CAPABILITY_STAGES.get(
                planned.capability,
                TaskStatus.RUNNING,
            )

            task.status = stage

            agent = ExecutorStageAgent(
                planned.registration.executor,
                stage,
            )

            try:
                result: StageAgentResult = agent.execute(
                    task,
                    context,
                )
            except Exception as exc:
                pipeline_result = PipelineStageResult(
                    stage=stage,
                    success=False,
                    error=str(exc),
                    agent=getattr(agent, "name", ""),
                    context_fingerprint=(
                        context.fingerprint if context else ""
                    ),
                )
            else:
                pipeline_result = PipelineStageResult(
                    stage=stage,
                    success=result.success,
                    output=result.output,
                    error=result.error,
                    agent=result.agent,
                    context_fingerprint=result.context_fingerprint,
                )

            results.append(pipeline_result)

            if not pipeline_result.success:
                task.status = TaskStatus.FAILED
                return results

        task.status = TaskStatus.COMPLETED
        return results

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
