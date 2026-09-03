from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from forge.intelligence.dependencies import DependencyGraph


@dataclass
class DependencyImpact:
    """Files potentially affected by changing a source."""

    source: str
    affected: list[str]


class DependencyAnalyzer:
    """Higher-level analysis over the repository dependency graph."""

    def __init__(self, graph: DependencyGraph):
        self.graph = graph

    def direct_dependencies(self, source: str) -> list[str]:
        return self.graph.dependencies_of(source)

    def direct_dependents(self, target: str) -> list[str]:
        return self.graph.dependents_of(target)

    def transitive_dependencies(self, source: str) -> list[str]:
        return self._walk(
            source,
            lambda node: self.graph.dependencies_of(node),
        )

    def transitive_dependents(self, target: str) -> list[str]:
        return self._walk(
            target,
            lambda node: self.graph.dependents_of(node),
        )

    def impact_analysis(self, source: str) -> DependencyImpact:
        affected = self.transitive_dependents(source)
        return DependencyImpact(
            source=source,
            affected=affected,
        )

    def cycles(self) -> list[list[str]]:
        """Find dependency cycles using depth-first search."""

        adjacency: dict[str, list[str]] = {}

        for dependency in self.graph.dependencies:
            adjacency.setdefault(dependency.source, []).append(
                dependency.target
            )
            adjacency.setdefault(dependency.target, [])

        visited: set[str] = set()
        active: list[str] = []
        active_set: set[str] = set()
        found: set[tuple[str, ...]] = set()
        result: list[list[str]] = []

        def visit(node: str) -> None:
            if node in active_set:
                index = active.index(node)
                cycle = active[index:] + [node]

                # Normalize rotation so the same cycle is not reported twice.
                body = cycle[:-1]
                rotations = [
                    tuple(body[i:] + body[:i])
                    for i in range(len(body))
                ]
                key = min(rotations)

                if key not in found:
                    found.add(key)
                    result.append(cycle)

                return

            if node in visited:
                return

            visited.add(node)
            active.append(node)
            active_set.add(node)

            for target in adjacency.get(node, []):
                visit(target)

            active.pop()
            active_set.remove(node)

        for node in adjacency:
            visit(node)

        return result

    def roots(self) -> list[str]:
        """Return nodes with no incoming dependencies."""

        nodes = self._nodes()
        incoming = {
            dependency.target
            for dependency in self.graph.dependencies
        }

        return sorted(nodes - incoming)

    def leaves(self) -> list[str]:
        """Return nodes with no outgoing dependencies."""

        nodes = self._nodes()
        outgoing = {
            dependency.source
            for dependency in self.graph.dependencies
        }

        return sorted(nodes - outgoing)

    def _nodes(self) -> set[str]:
        nodes: set[str] = set()

        for dependency in self.graph.dependencies:
            nodes.add(dependency.source)
            nodes.add(dependency.target)

        return nodes

    @staticmethod
    def _walk(
        start: str,
        next_nodes,
    ) -> list[str]:
        visited: set[str] = set()
        queue = list(next_nodes(start))

        result: list[str] = []

        while queue:
            node = queue.pop(0)

            if node == start or node in visited:
                continue

            visited.add(node)
            result.append(node)

            queue.extend(next_nodes(node))

        return result
