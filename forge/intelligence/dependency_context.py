from __future__ import annotations

from forge.intelligence.context import ContextItem, ContextPack
from forge.intelligence.repository import RepositoryIntelligence


class DependencyContextExpander:
    """Expand context with dependency-aware repository files."""

    def __init__(self, intelligence: RepositoryIntelligence):
        self.intelligence = intelligence

    def expand(
        self,
        pack: ContextPack,
        *,
        max_depth: int = 1,
    ) -> ContextPack:
        expanded = ContextPack(query=pack.query)

        for item in pack.sorted_items():
            expanded.add(item)

        existing = {item.path for item in expanded.items}

        for item in pack.sorted_items():
            if item.kind not in {"file", "symbol"}:
                continue

            for path, depth in self._walk(
                item.path, "dependencies", max_depth
            ):
                self._add(
                    expanded,
                    existing,
                    path,
                    f"dependency depth {depth}",
                    max(0.0, 65.0 - (depth - 1) * 10.0),
                )

            for path, depth in self._walk(
                item.path, "dependents", max_depth
            ):
                self._add(
                    expanded,
                    existing,
                    path,
                    f"dependent depth {depth}",
                    max(0.0, 55.0 - (depth - 1) * 10.0),
                )

        return expanded

    def _walk(
        self,
        source: str,
        direction: str,
        max_depth: int,
    ) -> list[tuple[str, int]]:
        if max_depth <= 0:
            return []

        if direction == "dependencies":
            getter = self.intelligence._direct_dependency_paths
        else:
            getter = self.intelligence._direct_dependent_paths

        results = []
        visited = {source}
        frontier = [source]

        for depth in range(1, max_depth + 1):
            next_frontier = []

            for current in frontier:
                for path in sorted(getter(current)):
                    if path in visited:
                        continue

                    visited.add(path)
                    results.append((path, depth))
                    next_frontier.append(path)

            frontier = next_frontier

            if not frontier:
                break

        return results

    @staticmethod
    def _add(
        pack: ContextPack,
        existing: set[str],
        path: str,
        reason: str,
        score: float,
    ) -> None:
        if path in existing:
            return

        existing.add(path)
        pack.add(
            ContextItem(
                path=path,
                kind="file",
                reason=reason,
                score=score,
            )
        )
