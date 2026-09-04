from __future__ import annotations

from dataclasses import dataclass

from forge.intelligence.budget import ContextBudget, ContextBudgetManager
from forge.intelligence.context import ContextPack, ContextQuery
from forge.intelligence.context_pack import DeterministicContextPack
from forge.intelligence.context_query import ContextQueryEngine
from forge.intelligence.dependency_context import DependencyContextExpander
from forge.intelligence.repository import RepositoryIntelligence
from forge.intelligence.test_context import TestContextSelector


@dataclass(frozen=True)
class AgentContext:
    """Repository-aware context prepared for an agent."""

    pack: ContextPack
    estimated_tokens: int
    fingerprint: str

    @property
    def items(self):
        return self.pack.items

    @property
    def files(self) -> list[str]:
        return self.pack.files


class AgentContextBuilder:
    """Build deterministic, budgeted repository context for agents."""

    def __init__(
        self,
        intelligence: RepositoryIntelligence,
        max_tokens: int = 4000,
    ):
        self.intelligence = intelligence
        self.budget = ContextBudgetManager(
            ContextBudget(max_tokens=max_tokens)
        )

    def build(
        self,
        task: str,
        target_files: tuple[str, ...] = (),
        target_symbols: tuple[str, ...] = (),
    ) -> AgentContext:
        query = ContextQuery(
            task=task,
            target_files=target_files,
            target_symbols=target_symbols,
        )

        pack = ContextQueryEngine(self.intelligence).query(query)

        pack = DependencyContextExpander(
            self.intelligence
        ).expand(pack)

        pack = TestContextSelector(
            self.intelligence
        ).select(pack)

        budgeted = self.budget.apply(pack)

        final_pack = DeterministicContextPack.normalize(
            budgeted.pack
        )

        fingerprint = DeterministicContextPack.fingerprint(
            final_pack
        ).value

        return AgentContext(
            pack=final_pack,
            estimated_tokens=budgeted.estimated_tokens,
            fingerprint=fingerprint,
        )
