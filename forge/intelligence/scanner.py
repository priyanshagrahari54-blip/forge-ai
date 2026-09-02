from dataclasses import dataclass, field
from pathlib import Path


IGNORED_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
}


@dataclass
class FileInfo:
    path: str
    extension: str
    size: int


@dataclass
class RepositoryMap:
    root: str
    files: list[FileInfo] = field(default_factory=list)
    directories: list[str] = field(default_factory=list)
    extensions: dict[str, int] = field(default_factory=dict)


class RepositoryScanner:
    def __init__(self, root: str = ".") -> None:
        self.root = Path(root).resolve()

    def scan(self) -> RepositoryMap:
        result = RepositoryMap(root=str(self.root))

        for path in self.root.rglob("*"):
            relative = path.relative_to(self.root)

            if any(
                part in IGNORED_DIRECTORIES
                for part in relative.parts
            ):
                continue

            if path.is_dir():
                result.directories.append(
                    str(relative)
                )
                continue

            if path.is_file():
                extension = path.suffix.lower()

                info = FileInfo(
                    path=str(relative),
                    extension=extension,
                    size=path.stat().st_size,
                )

                result.files.append(info)

                result.extensions[extension] = (
                    result.extensions.get(extension, 0) + 1
                )

        return result
