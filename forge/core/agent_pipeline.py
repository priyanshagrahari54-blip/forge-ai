from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Mapping

from forge.agents.registry import AgentRegistry
from forge.agents.planner import AgentPlan
from forge.agents.stage_executor import ExecutorStageAgent
from forge.agents.validator import AgentPlanValidator
from forge.core.pipeline_agents import StageAgent, StageAgentResult
from forge.core.task_engine import Task, TaskStatus
from forge.intelligence.agent_context import AgentContext
from forge.performance.metrics import MetricsRecorder


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
        "research": TaskStatus.RESEARCHING,
        "coding": TaskStatus.CODING,
        "testing": TaskStatus.TESTING,
        "debugging": TaskStatus.DEBUGGING,
        "review": TaskStatus.REVIEWING,
        "security": TaskStatus.REVIEWING,
        "documentation": TaskStatus.RUNNING,
        "git": TaskStatus.RUNNING,
        "consensus": TaskStatus.REVIEWING,
        "supervisor": TaskStatus.RUNNING,
    }

    def __init__(
        self,
        stage_handler: Callable[[Task, TaskStatus], str] | None = None,
        stage_agents: Mapping[TaskStatus, StageAgent] | None = None,
        context_provider: Callable[[Task], AgentContext] | None = None,
        agent_registry: AgentRegistry | None = None,
        metrics_recorder: MetricsRecorder | None = None,
        timer: Callable[[], float] | None = None,
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
        self.plan_validator = (
            AgentPlanValidator(agent_registry)
            if agent_registry is not None
            else None
        )
        self.metrics_recorder = metrics_recorder
        self.timer = timer or time.perf_counter

    def _record_metric(
        self,
        task: Task,
        stage: TaskStatus,
        *,
        agent: str,
        status: str,
        duration_ms: float,
        context: AgentContext | None,
    ) -> None:
        if self.metrics_recorder is None:
            return

        self.metrics_recorder.record(
            task_id=task.id,
            stage=stage.value,
            agent=agent,
            status=status,
            duration_ms=duration_ms,
            attempts=task.attempts,
            retries=max(0, task.attempts - 1),
            affected_files=tuple(context.files) if context else (),
        )

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

        if self.plan_validator is not None:
            validation = self.plan_validator.validate(plan)

            if not validation.valid:
                task.status = TaskStatus.FAILED

                return [
                    PipelineStageResult(
                        stage=TaskStatus.RUNNING,
                        success=False,
                        error="Invalid agent plan: " + "; ".join(
                            validation.messages
                        ),
                    )
                ]

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

            started = self.timer()

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

            duration_ms = (self.timer() - started) * 1000
            self._record_metric(
                task,
                stage,
                agent=pipeline_result.agent,
                status=(
                    "success"
                    if pipeline_result.success
                    else "failure"
                ),
                duration_ms=duration_ms,
                context=context,
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

            started = self.timer()
            result = self._execute_stage(
                task,
                stage,
                context,
            )
            duration_ms = (self.timer() - started) * 1000

            self._record_metric(
                task,
                stage,
                agent=result.agent,
                status=(
                    "success" if result.success else "failure"
                ),
                duration_ms=duration_ms,
                context=context,
            )
            results.append(result)

            if not result.success:
                task.status = TaskStatus.FAILED
                return results

        task.status = TaskStatus.COMPLETED
        return results
