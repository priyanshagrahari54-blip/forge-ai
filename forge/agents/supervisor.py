from __future__ import annotations

from forge.agents.execution import AgentExecutor, AgentRequest, AgentResponse
from forge.intelligence.agent_context import (
    AgentContext,
    AgentContextBuilder,
)
from forge.intelligence.repository import RepositoryIntelligence


class SupervisorAgent:
    """Metadata and context building for the supervisor orchestration agent."""

    name = "supervisor"

    def describe(self) -> str:
        return (
            "Responsible for multi-task orchestration: dependency resolution, "
            "retry logic, and coordinating execution of complex workflows."
        )

    def build_context(
        self,
        intelligence: RepositoryIntelligence,
        task: str,
        target_files: tuple[str, ...] = (),
        target_symbols: tuple[str, ...] = (),
        max_tokens: int = 4000,
    ) -> AgentContext:
        return AgentContextBuilder(
            intelligence,
            max_tokens=max_tokens,
        ).build(
            task=task,
            target_files=target_files,
            target_symbols=target_symbols,
        )


class SupervisorExecutor(AgentExecutor):
    """Deterministic, model-independent supervisor orchestration executor.

    Interprets a task description as a request for multi-task orchestration
    and returns a structured plan describing the workflow. When invoked with
    a supervisor instance, it can execute the workflow.

    Executes entirely locally and never calls an external provider.
    """

    name = "supervisor"

    def __init__(
        self,
        supervisor=None,
        intelligence: RepositoryIntelligence | None = None,
    ) -> None:
        self.supervisor = supervisor
        self.intelligence = intelligence

    def execute(self, request: AgentRequest) -> AgentResponse:
        try:
            plan = self._compile_plan(request)
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
            output=plan,
            agent=self.name,
            stage=request.stage,
            context_fingerprint=(
                request.context.fingerprint
                if request.context
                else ""
            ),
        )

    def _compile_plan(self, request: AgentRequest) -> str:
        task = request.task.description.lower()
        lines: list[str] = [
            "# Supervisor Orchestration Plan",
            "",
            f"Task: {request.task.description}",
            "",
        ]

        if self.supervisor is not None:
            ready = self.supervisor.get_ready_tasks()
            blocked = self.supervisor.get_blocked_tasks()
            failed = self.supervisor.get_failed_tasks()

            lines.extend(
                [
                    "## Current orchestration state",
                    f"- ready tasks: {len(ready)}",
                    f"- blocked tasks: {len(blocked)}",
                    f"- failed tasks: {len(failed)}",
                    "",
                ]
            )

            if ready:
                lines.append("## Ready tasks")
                for t in ready:
                    lines.append(f"- {t.id}: {t.description}")
                lines.append("")

            if blocked:
                lines.append("## Blocked tasks")
                for t in blocked:
                    deps = ", ".join(t.dependencies) if t.dependencies else "none"
                    lines.append(f"- {t.id}: {t.description} (waiting on: {deps})")
                lines.append("")

        operations = self._detect_operations(task)

        if not operations:
            lines.append("## Result")
            lines.append(
                "No orchestration operations were requested. "
                "Mention tasks, dependencies, retry, or workflow "
                "to trigger orchestration.",
            )
            return "\n".join(lines)

        lines.append("## Planned operations")
        for op in operations:
            lines.append(f"- {op}")

        return "\n".join(lines)

    @staticmethod
    def _detect_operations(task: str) -> list[str]:
        """Detect which orchestration operations the task is requesting."""
        operations: list[str] = []

        task_keywords = (
            "task",
            "tasks",
            "workflow",
            "multi-step",
            "multi step",
            "pipeline",
            "orchestrate",
            "orchestration",
        )
        if any(keyword in task for keyword in task_keywords):
            operations.append(
                "orchestrate: coordinate multiple tasks with dependency resolution"
            )

        dep_keywords = (
            "depend",
            "dependency",
            "dependencies",
            "order",
            "sequence",
            "after",
            "before",
        )
        if any(keyword in task for keyword in dep_keywords):
            operations.append(
                "resolve_dependencies: determine execution order from task dependencies"
            )

        retry_keywords = (
            "retry",
            "retries",
            "attempts",
            "resilient",
            "resilience",
            "failure handling",
        )
        if any(keyword in task for keyword in retry_keywords):
            operations.append(
                "retry: automatically retry failed tasks up to max_retries"
            )

        parallel_keywords = (
            "parallel",
            "concurrent",
            "simultaneously",
            "at the same time",
        )
        if any(keyword in task for keyword in parallel_keywords):
            operations.append(
                "parallel: execute independent tasks concurrently"
            )

        return operations