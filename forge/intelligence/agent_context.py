from __future__ import annotations

from dataclasses import dataclass

from forge.intelligence.budget import ContextBudget, ContextBudgetManager
from forge.intelligence.context import ContextItem, ContextPack, ContextQuery
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
        memory=None,
        project: str | None = None,
    ) -> AgentContext:
        """Build deterministic, budgeted repository context for an agent.

        ``memory``/``project`` are optional long-term-memory integration
        points: when supplied, relevant remembered knowledge is appended as
        ``kind="memory"`` context items so agents see what Forge already
        knows about the task alongside repository context.
        """
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

        # Deterministic fallback: when the relevance engine selects nothing
        # (e.g. a task with no symbol/keyword overlap), the agent still needs
        # repository context. Seed with the project's source files so the model
        # never receives an empty context, without dumping the whole repo.
        if not final_pack.items:
            source_files = sorted(self.intelligence.architecture.source_files)
            for path in source_files[: query.max_files]:
                final_pack.add(ContextItem(
                    path=path,
                    kind="file",
                    reason="repository fallback context",
                    score=1.0,
                ))

        if memory is not None:
            from forge.memory.integrations import recall_for_context

            for index, result in enumerate(
                    recall_for_context(memory, project or "default", task)):
                record = result.record
                final_pack.add(ContextItem(
                    path=f"memory:{record.id}",
                    kind="memory",
                    reason=f"remembered {record.memory_type} knowledge",
                    score=float(result.score),
                ))

        fingerprint = DeterministicContextPack.fingerprint(
            final_pack
        ).value

        return AgentContext(
            pack=final_pack,
            estimated_tokens=budgeted.estimated_tokens,
            fingerprint=fingerprint,
        )
