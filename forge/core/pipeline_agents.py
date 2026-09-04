from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from forge.core.task_engine import Task, TaskStatus
from forge.intelligence.agent_context import AgentContext
from forge.intelligence.repository import RepositoryIntelligence


@dataclass(frozen=True)
class StageAgentResult:
    agent: str
    stage: TaskStatus
    success: bool
    output: str = ""
    error: str = ""
    context_fingerprint: str = ""


class StageAgent(Protocol):
    name: str
    stage: TaskStatus

    def execute(
        self,
        task: Task,
        context: AgentContext | None = None,
    ) -> StageAgentResult:
        ...


class CallableStageAgent:
    """Adapter for a callable implementation of a pipeline stage."""

    def __init__(
        self,
        name: str,
        stage: TaskStatus,
        worker: Callable[..., str],
    ) -> None:
        self.name = name
        self.stage = stage
        self.worker = worker

    def execute(
        self,
        task: Task,
        context: AgentContext | None = None,
    ) -> StageAgentResult:
        try:
            try:
                output = self.worker(task, context)
            except TypeError:
                output = self.worker(task)
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
            agent=self.name,
            stage=self.stage,
            success=True,
            output=str(output),
            context_fingerprint=(
                context.fingerprint if context else ""
            ),
        )


class PlannerAgent(CallableStageAgent):
    def __init__(self, worker: Callable[..., str]) -> None:
        super().__init__("planner", TaskStatus.PLANNING, worker)


class CoderStageAgent(CallableStageAgent):
    def __init__(self, worker: Callable[..., str]) -> None:
        super().__init__("coder", TaskStatus.CODING, worker)


class TesterStageAgent(CallableStageAgent):
    __test__ = False

    def __init__(self, worker: Callable[..., str]) -> None:
        super().__init__("tester", TaskStatus.TESTING, worker)


class ReviewerStageAgent(CallableStageAgent):
    def __init__(self, worker: Callable[..., str]) -> None:
        super().__init__("reviewer", TaskStatus.REVIEWING, worker)


class ContextAwareStageAgent(CallableStageAgent):
    """Stage agent that builds repository context before execution."""

    def __init__(
        self,
        name: str,
        stage: TaskStatus,
        worker: Callable[..., str],
        intelligence: RepositoryIntelligence,
        max_tokens: int = 4000,
    ) -> None:
        super().__init__(name, stage, worker)
        self.intelligence = intelligence
        self.max_tokens = max_tokens

    def build_context(self, task: Task) -> AgentContext:
        from forge.intelligence.agent_context import AgentContextBuilder

        return AgentContextBuilder(
            self.intelligence,
            max_tokens=self.max_tokens,
        ).build(task=task.description)

    def execute(
        self,
        task: Task,
        context: AgentContext | None = None,
    ) -> StageAgentResult:
        context = context or self.build_context(task)
        return super().execute(task, context)
