from dataclasses import dataclass, field
from pathlib import Path

from forge.intelligence.scanner import (
    RepositoryMap,
    RepositoryScanner,
)
from forge.intelligence.python_parser import (
    PythonFileInfo,
    PythonParser,
)


@dataclass
class ProjectAnalysis:
    repository: RepositoryMap
    python_files: list[PythonFileInfo] = field(
        default_factory=list
    )


class ProjectAnalyzer:
    def __init__(self, root: str = ".") -> None:
        self.root = Path(root).resolve()
        self.scanner = RepositoryScanner(root)
        self.python_parser = PythonParser()

    def analyze(self) -> ProjectAnalysis:
        repository = self.scanner.scan()

        python_files: list[PythonFileInfo] = []

        for file in repository.files:

            if file.extension != ".py":
                continue

            path = self.root / file.path

            try:
                source = path.read_text(
                    encoding="utf-8"
                )

                parsed = self.python_parser.parse(
                    file.path,
                    source,
                )

                python_files.append(parsed)

            except (UnicodeDecodeError, SyntaxError):
                continue

        return ProjectAnalysis(
            repository=repository,
            python_files=python_files,
        )
