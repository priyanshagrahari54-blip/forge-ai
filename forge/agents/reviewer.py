from __future__ import annotations

from forge.intelligence.agent_context import AgentContext, AgentContextBuilder
from forge.intelligence.repository import RepositoryIntelligence


class ReviewerAgent:
    name = "reviewer"

    def describe(self) -> str:
        return "Responsible for independently reviewing changes."

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
