from __future__ import annotations

from forge.intelligence.context import ContextItem, ContextPack
from forge.intelligence.repository import RepositoryIntelligence


class TestContextSelector:
    __test__ = False
    """Add relevant tests to a repository context pack."""

    def __init__(self, intelligence: RepositoryIntelligence):
        self.intelligence = intelligence

    def select(self, pack: ContextPack) -> ContextPack:
        selected = ContextPack(query=pack.query)

        for item in pack.sorted_items():
            selected.add(item)

        existing = {item.path for item in selected.items}

        if not pack.query.include_tests:
            return selected

        for item in pack.sorted_items():
            if item.kind not in {"file", "symbol"}:
                continue

            for test in self.intelligence.affected_tests(item.path):
                if test in existing:
                    continue

                existing.add(test)
                selected.add(
                    ContextItem(
                        path=test,
                        kind="test",
                        reason=f"test coverage for {item.path}",
                        score=60.0,
                    )
                )

        return selected
