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
            parsed = self.parser.parse(relative, source)
        except (OSError, UnicodeDecodeError, SyntaxError):
            return

        for import_detail in parsed.import_details:
            module = import_detail.module

            if import_detail.level:
                target = self._resolve_relative_import(
                    relative,
                    module,
                    import_detail.level,
                )
            elif import_detail.names:
                target = self._resolve_from_import_target(
                    module,
                    import_detail.names,
                )
            else:
                target = module

            kind, resolved_path = self._resolve_import(
                target,
                relative,
            )

            graph.add(
                source=relative,
                target=target,
                kind=kind,
                resolved_path=resolved_path,
            )

    def _resolve_from_import_target(
        self,
        module: str,
        names: list[str],
    ) -> str:
        """Choose the most specific target for a from-import."""

        if not module:
            return names[0] if names else module

        # Prefer an imported name that is an actual repository module.
        for name in names:
            candidate = f"{module}.{name}"

            if self._resolve_module(candidate):
                return candidate

        return module

    def _resolve_relative_import(
        self,
        source_file: str,
        module: str,
        level: int,
    ) -> str:
        """Convert a relative import into a repository module path."""

        source = Path(source_file)
        package_parts = list(source.parent.parts)

        # level=1 means current package.
        remove = max(level - 1, 0)

        if remove:
            package_parts = package_parts[:-remove]

        if module:
            package_parts.extend(module.split("."))

        return ".".join(part for part in package_parts if part)

    def _resolve_import(
        self,
        target: str,
        source_file: str | None = None,
    ) -> tuple[str, str | None]:
        """Resolve a Python import to a repository file when possible."""

        # First try the import exactly as written.
        resolved = self._resolve_module(target)
        if resolved:
            return "internal", resolved

        # Handle imports such as:
        # from forge.core import state
        #
        # The parser records the module as forge.core.state.
        # Try the complete module first, then progressively shorter
        # module paths so package imports resolve correctly.
        parts = target.split(".")

        for index in range(len(parts) - 1, 0, -1):
            module = ".".join(parts[:index])
            resolved = self._resolve_module(module)

            if resolved:
                remainder = parts[index:]
                candidate = self._resolve_from_import(
                    resolved,
                    remainder,
                )

                if candidate:
                    return "internal", candidate

                return "internal", resolved

        return "external", None

    def _resolve_module(self, module: str) -> str | None:
        """Resolve a dotted Python module to .py or package __init__.py."""

        module_path = self.root.joinpath(*module.split("."))

        file_path = module_path.with_suffix(".py")

        if file_path.is_file():
            return file_path.relative_to(self.root).as_posix()

        init_path = module_path / "__init__.py"

        if init_path.is_file():
            return init_path.relative_to(self.root).as_posix()

        return None

    def _resolve_from_import(
        self,
        resolved_module: str,
        remainder: list[str],
    ) -> str | None:
        """Resolve imported names inside a resolved package/module."""

        if not remainder:
            return resolved_module

        base = self.root / Path(resolved_module)

        # If the resolved module is __init__.py, look beside it.
        if base.name == "__init__.py":
            base = base.parent
        else:
            base = base.parent / base.stem

        candidate = base.joinpath(*remainder)

        file_path = candidate.with_suffix(".py")
        if file_path.is_file():
            return file_path.relative_to(self.root).as_posix()

        init_path = candidate / "__init__.py"
        if init_path.is_file():
            return init_path.relative_to(self.root).as_posix()

        return None
