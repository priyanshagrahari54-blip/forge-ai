from __future__ import annotations

import ast
from dataclasses import dataclass, field


@dataclass
class PythonSymbol:
    name: str
    kind: str
    line: int


@dataclass
class PythonImport:
    module: str
    names: list[str] = field(default_factory=list)
    level: int = 0


@dataclass
class PythonFileInfo:
    path: str
    imports: list[str] = field(default_factory=list)
    import_details: list[PythonImport] = field(default_factory=list)
    symbols: list[PythonSymbol] = field(default_factory=list)


class PythonParser:
    def parse(self, path: str, source: str) -> PythonFileInfo:
        result = PythonFileInfo(path=path)
        tree = ast.parse(source, filename=path)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    result.imports.append(alias.name)
                    result.import_details.append(
                        PythonImport(
                            module=alias.name,
                            names=[],
                            level=0,
                        )
                    )

            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = [
                    alias.name
                    for alias in node.names
                    if alias.name != "*"
                ]

                result.import_details.append(
                    PythonImport(
                        module=module,
                        names=names,
                        level=node.level,
                    )
                )

                # Keep the original compatibility behavior:
                # from forge.core import state
                # -> "forge.core"
                if module:
                    result.imports.append(module)
                elif names:
                    result.imports.extend(names)

            elif isinstance(node, ast.FunctionDef):
                result.symbols.append(
                    PythonSymbol(
                        name=node.name,
                        kind="function",
                        line=node.lineno,
                    )
                )

            elif isinstance(node, ast.AsyncFunctionDef):
                result.symbols.append(
                    PythonSymbol(
                        name=node.name,
                        kind="async_function",
                        line=node.lineno,
                    )
                )

            elif isinstance(node, ast.ClassDef):
                result.symbols.append(
                    PythonSymbol(
                        name=node.name,
                        kind="class",
                        line=node.lineno,
                    )
                )

        return result
