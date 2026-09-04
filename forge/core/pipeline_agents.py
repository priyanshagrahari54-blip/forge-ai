from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from forge.core.task_engine import Task, TaskStatus


@dataclass(frozen=True)
class StageAgentResult:
    agent: str
    stage: TaskStatus
    success: bool
    output: str = ""
    error: str = ""


class StageAgent(Protocol):
    name: str
    stage: TaskStatus

    def execute(self, task: Task) -> StageAgentResult:
        ...


class CallableStageAgent:
    """Adapter for a callable implementation of a pipeline stage."""

    def __init__(
        self,
        name: str,
        stage: TaskStatus,
        worker: Callable[[Task], str],
    ) -> None:
        self.name = name
        self.stage = stage
        self.worker = worker

    def execute(self, task: Task) -> StageAgentResult:
        try:
            output = self.worker(task)
        except Exception as exc:
            return StageAgentResult(
                agent=self.name,
                stage=self.stage,
                success=False,
                error=str(exc),
            )

        return StageAgentResult(
            agent=self.name,
            stage=self.stage,
            success=True,
            output=str(output),
        )


class PlannerAgent(CallableStageAgent):
    def __init__(self, worker: Callable[[Task], str]) -> None:
        super().__init__("planner", TaskStatus.PLANNING, worker)


class CoderStageAgent(CallableStageAgent):
    def __init__(self, worker: Callable[[Task], str]) -> None:
        super().__init__("coder", TaskStatus.CODING, worker)


class TesterStageAgent(CallableStageAgent):
    __test__ = False

    def __init__(self, worker: Callable[[Task], str]) -> None:
        super().__init__("tester", TaskStatus.TESTING, worker)


class ReviewerStageAgent(CallableStageAgent):
    def __init__(self, worker: Callable[[Task], str]) -> None:
        super().__init__("reviewer", TaskStatus.REVIEWING, worker)
