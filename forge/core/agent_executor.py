from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from forge.core.task_engine import Task


@dataclass(frozen=True)
class AgentExecutionResult:
    """Structured result returned by an agent execution."""

    success: bool
    output: str = ""
    error: str = ""
    agent: str = ""


class AgentExecutor(Protocol):
    """Interface implemented by concrete Forge agent executors."""

    def execute(self, task: Task) -> AgentExecutionResult:
        """Execute a task and return a structured result."""
        ...


class CallableAgentExecutor:
    """Adapter that turns a callable into an AgentExecutor."""

    def __init__(self, worker, agent_name: str = "agent") -> None:
        self.worker = worker
        self.agent_name = agent_name

    def execute(self, task: Task) -> AgentExecutionResult:
        try:
            output = self.worker(task)
        except Exception as exc:
            return AgentExecutionResult(
                success=False,
                error=str(exc),
                agent=self.agent_name,
            )

        return AgentExecutionResult(
            success=True,
            output=str(output),
            agent=self.agent_name,
        )
