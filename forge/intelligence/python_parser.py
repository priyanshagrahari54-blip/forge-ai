import ast
from dataclasses import dataclass, field


@dataclass
class PythonSymbol:
    name: str
    kind: str
    line: int


@dataclass
class PythonFileInfo:
    path: str
    imports: list[str] = field(default_factory=list)
    symbols: list[PythonSymbol] = field(default_factory=list)


class PythonParser:
    def parse(
        self,
        path: str,
        source: str,
    ) -> PythonFileInfo:

        result = PythonFileInfo(path=path)

        tree = ast.parse(source, filename=path)

        for node in ast.walk(tree):

            if isinstance(node, ast.Import):
                for name in node.names:
                    result.imports.append(name.name)

            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    result.imports.append(node.module)

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
