from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from forge.intelligence.architecture import (
    ArchitectureAnalyzer,
    ArchitectureReport,
)
from forge.intelligence.dependencies import (
    DependencyGraph,
    DependencyIndexer,
)
from forge.intelligence.dependency_analysis import DependencyAnalyzer
from forge.intelligence.runtime_detection import (
    RuntimeDetector,
    RuntimeReport,
)
from forge.intelligence.symbols import SymbolIndex, SymbolIndexer
from forge.intelligence.test_mapping import TestMapping, TestMapper


@dataclass
class RepositoryIntelligence:
    """Unified repository understanding for Forge agents."""

    root: Path
    symbols: SymbolIndex
    dependencies: DependencyGraph
    dependency_analysis: DependencyAnalyzer
    architecture: ArchitectureReport
    tests: TestMapping
    runtime: RuntimeReport

    @classmethod
    def build(cls, root: str | Path = ".") -> "RepositoryIntelligence":
        project_root = Path(root).resolve()

        symbols = SymbolIndexer(project_root).build()
        dependencies = DependencyIndexer(project_root).build()
        dependency_analysis = DependencyAnalyzer(dependencies)
        architecture = ArchitectureAnalyzer(project_root).analyze()

        tests = TestMapper(
            project_root,
            dependency_graph=dependencies,
        ).build()

        runtime = RuntimeDetector(project_root).detect()

        return cls(
            root=project_root,
            symbols=symbols,
            dependencies=dependencies,
            dependency_analysis=dependency_analysis,
            architecture=architecture,
            tests=tests,
            runtime=runtime,
        )

    def source_context(self, source: str) -> dict:
        """Return the most useful intelligence for a source file."""

        direct_dependencies = self._direct_dependency_paths(source)
        direct_dependents = self._direct_dependent_paths(source)
        transitive_dependencies = self._transitive_dependency_paths(source)
        transitive_dependents = self._transitive_dependent_paths(source)

        return {
            "source": source,
            "module": self._module_name(source),
            "symbols": [
                {
                    "name": symbol.name,
                    "kind": symbol.kind,
                    "line": symbol.line,
                }
                for symbol in self.symbols.by_file(source)
            ],
            "dependencies": direct_dependencies,
            "dependents": direct_dependents,
            "transitive_dependencies": transitive_dependencies,
            "transitive_dependents": transitive_dependents,
            "tests": self.tests.tests_for_source(source),
            "package": self.architecture.package_for_file(source),
        }

    def affected_tests(self, source: str) -> list[str]:
        """Return tests potentially affected by a source change."""

        return self.tests.tests_for_source(source)

    def impact(self, source: str) -> list[str]:
        """Return production/source files potentially affected by a change."""

        affected = self._transitive_dependent_paths(source)

        return [
            path
            for path in affected
            if path not in self.architecture.test_files
        ]

    def _direct_dependency_paths(self, source: str) -> list[str]:
        """Return direct dependency file paths for a source file."""

        result: list[str] = []

        for dependency in self.dependencies.dependencies:
            if dependency.source != source:
                continue

            if dependency.resolved_path is not None:
                result.append(dependency.resolved_path)

        return result

    def _direct_dependent_paths(self, source: str) -> list[str]:
        """Return files that directly depend on a source file."""

        result: list[str] = []

        for dependency in self.dependencies.dependencies:
            if dependency.resolved_path == source:
                result.append(dependency.source)

        return result

    def _transitive_dependency_paths(self, source: str) -> list[str]:
        """Walk dependencies using resolved repository file paths."""

        visited: set[str] = set()
        queue = self._direct_dependency_paths(source)
        result: list[str] = []

        while queue:
            current = queue.pop(0)

            if current == source or current in visited:
                continue

            visited.add(current)
            result.append(current)
            queue.extend(self._direct_dependency_paths(current))

        return result

    def _transitive_dependent_paths(self, source: str) -> list[str]:
        """Walk dependents using resolved repository file paths."""

        visited: set[str] = set()
        queue = self._direct_dependent_paths(source)
        result: list[str] = []

        while queue:
            current = queue.pop(0)

            if current == source or current in visited:
                continue

            visited.add(current)
            result.append(current)
            queue.extend(self._direct_dependent_paths(current))

        return result

    def _module_name(self, source: str) -> str | None:
        """Convert a repository Python file path to its module name."""

        path = Path(source)

        if path.suffix != ".py":
            return None

        parts = list(path.with_suffix("").parts)

        if not parts:
            return None

        if parts[-1] == "__init__":
            parts = parts[:-1]

        if not parts:
            return None

        return ".".join(parts)

    def _paths_from_modules(self, modules: list[str]) -> list[str]:
        """Convert dependency module names to repository file paths."""

        result: list[str] = []

        for module in modules:
            path = self._module_to_path(module)

            if path is not None:
                result.append(path)

        return result

    def _module_to_path(self, module: str) -> str | None:
        """Resolve a module name to its repository source path."""

        candidate = self.root.joinpath(*module.split("."))

        file_path = candidate.with_suffix(".py")

        if file_path.is_file():
            return file_path.relative_to(self.root).as_posix()

        init_path = candidate / "__init__.py"

        if init_path.is_file():
            return init_path.relative_to(self.root).as_posix()

        return None

    def summary(self) -> dict:
        """Return a compact machine-readable repository summary."""

        return {
            "root": str(self.root),
            "symbol_count": len(self.symbols.symbols),
            "dependency_count": len(
                self.dependencies.dependencies
            ),
            "package_count": len(
                self.architecture.packages
            ),
            "source_file_count": len(
                self.architecture.source_files
            ),
            "test_file_count": len(
                self.architecture.test_files
            ),
            "entry_points": list(
                self.architecture.entry_points
            ),
            "project_types": list(
                self.runtime.project_type
            ),
            "test_commands": [
                command.command
                for command in self.runtime.commands_for("test")
            ],
            "cycles": self.dependency_analysis.cycles(),
        }
