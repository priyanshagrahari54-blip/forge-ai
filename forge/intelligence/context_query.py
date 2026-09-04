from __future__ import annotations

import re

from forge.intelligence.context import ContextItem, ContextPack, ContextQuery
from forge.intelligence.repository import RepositoryIntelligence


class ContextQueryEngine:
    """Select initial repository context for an agent task."""

    def __init__(self, intelligence: RepositoryIntelligence):
        self.intelligence = intelligence

    def query(self, request: ContextQuery) -> ContextPack:
        pack = ContextPack(query=request)

        # Explicit files are always highest-priority context.
        for path in request.target_files:
            if self._is_repository_file(path):
                pack.add(
                    ContextItem(
                        path=path,
                        kind="target",
                        reason="explicit target file",
                        score=100.0,
                    )
                )

        # Explicit symbols are resolved through the unified symbol index.
        for symbol_name in request.target_symbols:
            for symbol in self.intelligence.symbols.by_name(symbol_name):
                pack.add(
                    ContextItem(
                        path=symbol.file,
                        kind="symbol",
                        symbol=symbol.name,
                        reason="explicit target symbol",
                        score=95.0,
                    )
                )

        # Match meaningful words from the task against repository symbols.
        task_terms = self._terms(request.task)

        for symbol in self.intelligence.symbols.symbols:
            score = self._symbol_score(symbol.name, task_terms)

            if score > 0:
                pack.add(
                    ContextItem(
                        path=symbol.file,
                        kind="symbol",
                        symbol=symbol.name,
                        reason="task/symbol match",
                        score=score,
                    )
                )

        # Expand context around selected source files.
        initial_files = list(pack.files)

        if request.include_dependencies:
            for path in initial_files:
                for dependency in self.intelligence._transitive_dependency_paths(path):
                    pack.add(
                        ContextItem(
                            path=dependency,
                            kind="dependency",
                            reason=f"dependency of {path}",
                            score=40.0,
                        )
                    )

        if request.include_dependents:
            for path in initial_files:
                for dependent in self.intelligence._transitive_dependent_paths(path):
                    pack.add(
                        ContextItem(
                            path=dependent,
                            kind="dependent",
                            reason=f"dependent of {path}",
                            score=35.0,
                        )
                    )

        # Add affected tests separately so agents can validate changes.
        if request.include_tests:
            for path in initial_files:
                for test in self.intelligence.affected_tests(path):
                    pack.add(
                        ContextItem(
                            path=test,
                            kind="test",
                            reason=f"test coverage for {path}",
                            score=50.0,
                        )
                    )

        pack.items = pack.sorted_items()[: request.max_files]
        return pack

    def _is_repository_file(self, path: str) -> bool:
        return path in self.intelligence.symbols.by_file(path) or any(
            file == path for file in self.intelligence.architecture.source_files
        )

    @staticmethod
    def _terms(text: str) -> set[str]:
        return {
            term
            for term in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text.lower())
            if len(term) >= 3
        }

    @staticmethod
    def _symbol_score(name: str, terms: set[str]) -> float:
        normalized = name.lower()

        if normalized in terms:
            return 90.0

        score = 0.0

        for term in terms:
            if term in normalized:
                score = max(score, 70.0)

        return score
