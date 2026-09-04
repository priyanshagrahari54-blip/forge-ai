from __future__ import annotations

from dataclasses import dataclass

from forge.intelligence.context import ContextItem, ContextPack


@dataclass(frozen=True)
class ContextBudget:
    """Maximum estimated token budget for a context pack."""

    max_tokens: int = 4000


@dataclass(frozen=True)
class BudgetedContext:
    """Context pack after applying a token budget."""

    pack: ContextPack
    estimated_tokens: int


class ContextBudgetManager:
    """Estimate and limit context size deterministically."""

    def __init__(self, budget: ContextBudget | None = None):
        self.budget = budget or ContextBudget()

    @staticmethod
    def estimate_item_tokens(item: ContextItem) -> int:
        """Estimate tokens represented by a context item.

        Until file contents are loaded, use metadata-based estimation.
        This intentionally stays deterministic and inexpensive.
        """
        base = 50

        if item.symbol:
            base += 25

        if item.start_line is not None and item.end_line is not None:
            lines = max(1, item.end_line - item.start_line + 1)
            base += lines * 10

        return base

    def estimate(self, pack: ContextPack) -> int:
        return sum(
            self.estimate_item_tokens(item)
            for item in pack.sorted_items()
        )

    def apply(self, pack: ContextPack) -> BudgetedContext:
        """Keep the highest-value context that fits the budget."""
        selected: list[ContextItem] = []
        used = 0

        for item in pack.sorted_items():
            cost = self.estimate_item_tokens(item)

            if used + cost > self.budget.max_tokens:
                continue

            selected.append(item)
            used += cost

        result = ContextPack(query=pack.query)

        for item in selected:
            result.add(item)

        return BudgetedContext(
            pack=result,
            estimated_tokens=used,
        )
