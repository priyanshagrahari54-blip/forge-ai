from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from forge.intelligence.gitignore import GitIgnoreMatcher
from forge.intelligence.python_parser import PythonParser


@dataclass
class Symbol:
    """Normalized repository-wide source-code symbol."""

    name: str
    kind: str
    file: str
    line: int

    @property
    def qualified_name(self) -> str:
        return f"{self.file}:{self.name}"


@dataclass
class SymbolIndex:
    """Repository-wide index of discovered symbols."""

    symbols: list[Symbol] = field(default_factory=list)

    def add(self, symbol: Symbol) -> None:
        self.symbols.append(symbol)

    def by_name(self, name: str) -> list[Symbol]:
        return [
            symbol
            for symbol in self.symbols
            if symbol.name == name
        ]

    def by_file(self, file: str) -> list[Symbol]:
        return [
            symbol
            for symbol in self.symbols
            if symbol.file == file
        ]

    def by_kind(self, kind: str) -> list[Symbol]:
        return [
            symbol
            for symbol in self.symbols
            if symbol.kind == kind
        ]

    def find(self, query: str) -> list[Symbol]:
        """Find symbols by name or qualified name."""
        query = query.strip()

        return [
            symbol
            for symbol in self.symbols
            if (
                symbol.name == query
                or symbol.qualified_name == query
            )
        ]


class SymbolIndexer:
    """Build a unified symbol index from the repository."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.parser = PythonParser()
        self.gitignore = GitIgnoreMatcher(self.root)

    def build(self) -> SymbolIndex:
        index = SymbolIndex()

        for path in self.root.rglob("*.py"):
            if self._should_ignore(path):
                continue

            self._index_python_file(path, index)

        return index

    def _should_ignore(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            return True

        return self.gitignore.is_ignored(relative.as_posix())

    def _index_python_file(
        self,
        path: Path,
        index: SymbolIndex,
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

        for symbol in parsed.symbols:
            index.add(
                Symbol(
                    name=symbol.name,
                    kind=symbol.kind,
                    file=relative,
                    line=symbol.line,
                )
            )
