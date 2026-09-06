from __future__ import annotations

from forge.agents.execution import AgentExecutor, AgentRequest, AgentResponse
from forge.intelligence.agent_context import (
    AgentContext,
    AgentContextBuilder,
)
from forge.intelligence.repository import RepositoryIntelligence


class ResearchAgent:
    """Metadata and context building for the research agent."""

    name = "researcher"

    def describe(self) -> str:
        return (
            "Responsible for researching the repository and locating "
            "the most relevant files, symbols, and tests for a task."
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


class ResearchExecutor(AgentExecutor):
    """Deterministic, model-independent research executor.

    Produces a markdown research report from the agent context. When a
    RepositoryIntelligence instance is available, each relevant file is
    enriched with its symbols and tests. Executes entirely locally and
    never calls an external provider.
    """

    name = "researcher"

    def __init__(
        self,
        intelligence: RepositoryIntelligence | None = None,
    ) -> None:
        self.intelligence = intelligence

    def execute(self, request: AgentRequest) -> AgentResponse:
        try:
            report = self._compile_report(request)
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
            output=report,
            agent=self.name,
            stage=request.stage,
            context_fingerprint=(
                request.context.fingerprint
                if request.context
                else ""
            ),
        )

    def _compile_report(self, request: AgentRequest) -> str:
        context = request.context

        if context is None:
            if self.intelligence is None:
                raise RuntimeError(
                    "ResearchExecutor requires an AgentContext or "
                    "a RepositoryIntelligence instance."
                )

            context = AgentContextBuilder(
                self.intelligence
            ).build(task=request.task.description)

        lines: list[str] = [
            "# Research Report",
            "",
            f"Task: {request.task.description}",
            f"Context fingerprint: {context.fingerprint}",
            "",
            "## Relevant repository files",
        ]

        files = sorted(set(context.files))

        if not files:
            lines.append("- (no repository context selected)")
        else:
            for file in files:
                lines.append(f"- {file}")

                if self.intelligence is None:
                    continue

                info = self.intelligence.source_context(file)

                symbols = [
                    f"{symbol['kind']} {symbol['name']}"
                    for symbol in info["symbols"]
                ]

                tests = sorted(set(info["tests"]))

                if symbols:
                    lines.append(f"    symbols: {', '.join(symbols)}")

                if tests:
                    lines.append(f"    tests: {', '.join(tests)}")

        lines.extend(
            [
                "",
                "## Findings",
                (
                    "The files above are the most relevant for the "
                    "requested task; no code changes were made."
                ),
            ]
        )

        return "\n".join(lines)