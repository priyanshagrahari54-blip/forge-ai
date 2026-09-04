from __future__ import annotations

from forge.agents.execution import AgentExecutor, AgentRequest
from forge.core.pipeline_agents import StageAgentResult
from forge.core.task_engine import Task, TaskStatus
from forge.intelligence.agent_context import AgentContext


class ExecutorStageAgent:
    """Adapter that exposes an AgentExecutor as a pipeline StageAgent."""

    def __init__(
        self,
        executor: AgentExecutor,
        stage: TaskStatus,
    ) -> None:
        self.executor = executor
        self.name = executor.name
        self.stage = stage

    def execute(
        self,
        task: Task,
        context: AgentContext | None = None,
    ) -> StageAgentResult:
        request = AgentRequest(
            task=task,
            stage=self.stage,
            context=context,
        )

        try:
            response = self.executor.execute(request)
        except Exception as exc:
            return StageAgentResult(
                agent=self.name,
                stage=self.stage,
                success=False,
                error=str(exc),
                context_fingerprint=(
                    context.fingerprint if context else ""
                ),
            )

        return StageAgentResult(
            agent=response.agent or self.name,
            stage=response.stage or self.stage,
            success=response.success,
            output=response.output,
            error=response.error,
            context_fingerprint=(
                response.context_fingerprint
                or (context.fingerprint if context else "")
            ),
        )
