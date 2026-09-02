from pathlib import Path


class FileSystemTool:
    def __init__(self, root: str = ".") -> None:
        self.root = Path(root).resolve()

    def _safe_path(self, path: str) -> Path:
        target = (self.root / path).resolve()

        try:
            target.relative_to(self.root)
        except ValueError:
            raise PermissionError(
                "Path escapes the allowed project directory."
            )

        return target

    def read(self, path: str) -> str:
        target = self._safe_path(path)

        if not target.exists():
            raise FileNotFoundError(path)

        if not target.is_file():
            raise IsADirectoryError(path)

        return target.read_text(encoding="utf-8")

    def write(self, path: str, content: str) -> None:
        target = self._safe_path(path)

        target.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        target.write_text(
            content,
            encoding="utf-8",
        )

    def exists(self, path: str) -> bool:
        return self._safe_path(path).exists()

    def delete(self, path: str) -> None:
        target = self._safe_path(path)

        if target.is_file():
            target.unlink()
        else:
            raise IsADirectoryError(
                "Directory deletion is not supported by this tool."
            )
