from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from forge.intelligence.gitignore import GitIgnoreMatcher
from forge.intelligence.python_parser import PythonParser


@dataclass
class Dependency:
    """A dependency from one source file to another module."""

    source: str
    target: str
    kind: str = "external"
    resolved_path: str | None = None


@dataclass
class DependencyGraph:
    """Repository-wide module dependency graph."""

    dependencies: list[Dependency] = field(default_factory=list)

    def add(
        self,
        source: str,
        target: str,
        kind: str = "external",
        resolved_path: str | None = None,
    ) -> None:
        dependency = Dependency(
            source=source,
            target=target,
            kind=kind,
            resolved_path=resolved_path,
        )

        if dependency not in self.dependencies:
            self.dependencies.append(dependency)

    def dependencies_of(self, source: str) -> list[str]:
        return [
            dependency.target
            for dependency in self.dependencies
            if dependency.source == source
        ]

    def dependents_of(self, target: str) -> list[str]:
        return [
            dependency.source
            for dependency in self.dependencies
            if dependency.target == target
        ]

    def internal_dependencies_of(self, source: str) -> list[Dependency]:
        return [
            dependency
            for dependency in self.dependencies
            if dependency.source == source
            and dependency.kind == "internal"
        ]

    def external_dependencies_of(self, source: str) -> list[Dependency]:
        return [
            dependency
            for dependency in self.dependencies
            if dependency.source == source
            and dependency.kind == "external"
        ]


class DependencyIndexer:
    """Build a dependency graph from Python imports."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.parser = PythonParser()
        self.gitignore = GitIgnoreMatcher(self.root)

    def build(self) -> DependencyGraph:
        graph = DependencyGraph()

        for path in self.root.rglob("*.py"):
            if self._should_ignore(path):
                continue

            self._index_python_file(path, graph)

        return graph

    def _should_ignore(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            return True

        return self.gitignore.is_ignored(relative.as_posix())

    def _index_python_file(
        self,
        path: Path,
        graph: DependencyGraph,
    ) -> None:
        try:
            relative = path.relative_to(self.root).as_posix()
            source = path.read_text(encoding="utf-8")

            parsed = self.parser.parse(
                relative,
                source,
            )

        except (OSError, UnicodeDecodeError, SyntaxError):
            return

        for target in parsed.imports:
            kind, resolved_path = self._resolve_import(target)

            graph.add(
                source=relative,
                target=target,
                kind=kind,
                resolved_path=resolved_path,
            )

    def _resolve_import(
        self,
        target: str,
    ) -> tuple[str, str | None]:
        """Resolve an import to a repository file when possible."""

        module_parts = target.split(".")

        module_path = self.root.joinpath(*module_parts)

        # Example:
        # forge.core
        # -> forge/core.py
        if module_path.with_suffix(".py").is_file():
            relative = module_path.with_suffix(".py").relative_to(
                self.root
            )
            return "internal", relative.as_posix()

        # Example:
        # forge.core
        # -> forge/core/__init__.py
        init_path = module_path / "__init__.py"

        if init_path.is_file():
            relative = init_path.relative_to(self.root)
            return "internal", relative.as_posix()

        return "external", None