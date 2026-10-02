from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from forge.intelligence.dependencies import DependencyGraph
from forge.intelligence.gitignore import GitIgnoreMatcher


@dataclass
class TestMapping:
    __test__ = False
    """Mapping between source files and relevant test files."""

    source_to_tests: dict[str, list[str]] = field(default_factory=dict)
    test_to_sources: dict[str, list[str]] = field(default_factory=dict)

    def add(self, source: str, test: str) -> None:
        self.source_to_tests.setdefault(source, [])
        self.test_to_sources.setdefault(test, [])

        if test not in self.source_to_tests[source]:
            self.source_to_tests[source].append(test)

        if source not in self.test_to_sources[test]:
            self.test_to_sources[test].append(source)

    def tests_for_source(self, source: str) -> list[str]:
        return list(self.source_to_tests.get(source, []))

    def sources_for_test(self, test: str) -> list[str]:
        return list(self.test_to_sources.get(test, []))


class TestMapper:
    __test__ = False
    """Build source-to-test relationships."""

    def __init__(
        self,
        root: str | Path,
        dependency_graph: DependencyGraph | None = None,
    ):
        self.root = Path(root).resolve()
        self.gitignore = GitIgnoreMatcher(self.root)
        self.dependency_graph = dependency_graph

    def build(self) -> TestMapping:
        mapping = TestMapping()

        source_files, test_files = self._discover_files()

        # Performance optimization (Bolt ⚡): Pre-index source files by stem
        # to convert O(N*M) stem matching loops into O(1) dictionary lookups.
        source_by_stem: dict[str, list[str]] = {}
        for source in source_files:
            source_stem = Path(source).stem
            source_by_stem.setdefault(source_stem, []).append(source)

        for test in test_files:
            related = self._match_test_to_sources(
                test,
                source_files,
                source_by_stem=source_by_stem,
            )

            for source in related:
                mapping.add(source, test)

        self._add_dependency_relationships(mapping, source_files, test_files)

        return mapping

    # Performance optimization (Bolt ⚡): Single-pass directory discovery avoids duplicate rglob calls
    def _discover_files(self) -> tuple[list[str], list[str]]:
        source_files: list[str] = []
        test_files: list[str] = []

        for path in self.root.rglob("*.py"):
            if not path.is_file():
                continue

            relative = self._relative(path)

            if relative is None or self.gitignore.is_ignored(relative):
                continue

            if self._is_test_file(path):
                test_files.append(relative)
            else:
                source_files.append(relative)

        return sorted(source_files), sorted(test_files)

    def _source_files(self) -> list[str]:
        source_files, _ = self._discover_files()
        return source_files

    def _test_files(self) -> list[str]:
        _, test_files = self._discover_files()
        return test_files

    def _match_test_to_sources(
        self,
        test: str,
        source_files: list[str],
        source_by_stem: dict[str, list[str]] | None = None,
    ) -> list[str]:
        test_path = Path(test)
        test_stem = test_path.stem

        if test_stem.startswith("test_"):
            target_name = test_stem[5:]
        elif test_stem.endswith("_test"):
            target_name = test_stem[:-5]
        else:
            target_name = test_stem

        if source_by_stem is not None:
            # O(1) lookup when pre-indexed stem map is provided
            return list(source_by_stem.get(target_name, []))

        matches: list[str] = []

        for source in source_files:
            source_path = Path(source)

            if source_path.stem == target_name:
                matches.append(source)

        return matches

    def _add_dependency_relationships(
        self,
        mapping: TestMapping,
        source_files: list[str],
        test_files: list[str],
    ) -> None:
        if self.dependency_graph is None:
            return

        source_set = set(source_files)

        for test in test_files:
            queue = deque([test])
            visited: set[str] = set()

            while queue:
                current = queue.popleft()

                if current in visited:
                    continue

                visited.add(current)

                for dependency in self.dependency_graph.dependencies_of(current):
                    if dependency in source_set:
                        mapping.add(dependency, test)

                    if dependency not in visited:
                        queue.append(dependency)

    def _is_test_file(self, path: Path) -> bool:
        parts = {part.lower() for part in path.parts}

        if "test" in parts or "tests" in parts:
            return True

        name = path.name.lower()

        return (
            name.startswith("test_")
            or name.endswith("_test.py")
        )

    def _relative(self, path: Path) -> str | None:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return None


@dataclass
class RegressionScope:
    """Tests that should be considered for a changed source file."""

    changed_source: str
    tests: list[str]


class RegressionSelector:
    """Select tests affected by a source change."""

    def __init__(self, mapping: TestMapping):
        self.mapping = mapping

    def select(self, changed_source: str) -> RegressionScope:
        tests = self.mapping.tests_for_source(changed_source)

        return RegressionScope(
            changed_source=changed_source,
            tests=sorted(set(tests)),
        )
