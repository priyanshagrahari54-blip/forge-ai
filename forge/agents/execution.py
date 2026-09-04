from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from forge.core.task_engine import Task, TaskStatus
from forge.intelligence.agent_context import AgentContext


@dataclass(frozen=True)
class AgentRequest:
    """Structured input supplied to an agent."""

    task: Task
    stage: TaskStatus
    context: AgentContext | None = None
    instructions: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentResponse:
    """Structured result returned by an agent."""

    success: bool
    output: str = ""
    error: str = ""
    agent: str = ""
    stage: TaskStatus | None = None
    context_fingerprint: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)


class AgentExecutor:
    """Common execution contract for real Forge agents."""

    name: str

    def execute(self, request: AgentRequest) -> AgentResponse:
        raise NotImplementedError


class CallableAgentExecutor(AgentExecutor):
    """Adapter that turns a Python callable into an AgentExecutor."""

    def __init__(self, name: str, worker) -> None:
        self.name = name
        self.worker = worker

    def execute(self, request: AgentRequest) -> AgentResponse:
        try:
            output = self.worker(request)
        except Exception as exc:
            return AgentResponse(
                success=False,
                error=str(exc),
                agent=self.name,
                stage=request.stage,
                context_fingerprint=(
                    request.context.fingerprint
                    if request.context
                    else ""
                ),
            )

        return AgentResponse(
            success=True,
            output=str(output),
            agent=self.name,
            stage=request.stage,
            context_fingerprint=(
                request.context.fingerprint
                if request.context
                else ""
            ),
        )
