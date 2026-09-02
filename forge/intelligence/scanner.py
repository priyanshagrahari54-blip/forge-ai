from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from forge.intelligence.gitignore import GitIgnoreMatcher


IGNORED_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "build",
    "dist",
}


@dataclass
class FileInfo:
    path: str
    size: int
    extension: str


@dataclass
class RepositoryMap:
    root: str
    files: list[FileInfo] = field(default_factory=list)
    directories: list[str] = field(default_factory=list)
    extensions: dict[str, int] = field(default_factory=dict)

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def directory_count(self) -> int:
        return len(self.directories)


class RepositoryScanner:
    """Scan a repository while respecting .gitignore."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.gitignore = GitIgnoreMatcher(self.root)

    def scan(self) -> RepositoryMap:
        repository = RepositoryMap(root=str(self.root))

        for path in self.root.rglob("*"):
            relative = path.relative_to(self.root)

            # Ignore .git and other known generated/vendor directories.
            if any(part in IGNORED_DIRECTORIES for part in relative.parts):
                continue

            # Respect repository .gitignore.
            if self.gitignore.is_ignored(relative):
                continue

            if path.is_dir():
                repository.directories.append(relative.as_posix())
                continue

            if not path.is_file():
                continue

            extension = path.suffix.lower()

            file_info = FileInfo(
                path=relative.as_posix(),
                size=path.stat().st_size,
                extension=extension,
            )

            repository.files.append(file_info)
            repository.extensions[extension] = (
                repository.extensions.get(extension, 0) + 1
            )

        return repository