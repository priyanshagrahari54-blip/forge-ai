from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from forge.intelligence.context import ContextItem, ContextPack


@dataclass(frozen=True)
class SymbolRange:
    path: str
    name: str
    kind: str
    start_line: int
    end_line: int


class SymbolContextSelector:
    """Enrich context with precise source ranges for Python symbols/imports."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def select(self, pack: ContextPack) -> ContextPack:
        enriched = ContextPack(query=pack.query)

        for item in pack.items:
            if item.kind != "symbol" or not item.symbol:
                enriched.add(item)
                continue

            symbol_range = self._find_symbol(item.path, item.symbol)

            if symbol_range is None:
                enriched.add(item)
                continue

            enriched.add(
                ContextItem(
                    path=item.path,
                    kind=item.kind,
                    symbol=item.symbol,
                    reason=item.reason,
                    score=item.score,
                    start_line=symbol_range.start_line,
                    end_line=symbol_range.end_line,
                )
            )

        return enriched

    def _find_symbol(self, path: str, name: str) -> SymbolRange | None:
        file_path = self.root / path

        if not file_path.is_file() or file_path.suffix != ".py":
            return None

        try:
            source = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(file_path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            return None

        for node in ast.walk(tree):
            if not isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                continue

            if node.name != name:
                continue

            end_line = getattr(node, "end_lineno", node.lineno)

            if isinstance(node, ast.ClassDef):
                kind = "class"
            elif isinstance(node, ast.AsyncFunctionDef):
                kind = "async_function"
            else:
                kind = "function"

            return SymbolRange(
                path=path,
                name=name,
                kind=kind,
                start_line=node.lineno,
                end_line=end_line,
            )

        return None
