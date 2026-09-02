from pathlib import Path


class SearchTool:
    def __init__(self, root: str = ".") -> None:
        self.root = Path(root).resolve()

    def text(
        self,
        query: str,
        extensions: tuple[str, ...] = (
            ".py",
            ".js",
            ".ts",
            ".tsx",
            ".jsx",
            ".json",
            ".yaml",
            ".yml",
            ".md",
        ),
    ) -> list[dict[str, object]]:

        if not query:
            return []

        results: list[dict[str, object]] = []

        for path in self.root.rglob("*"):

            if not path.is_file():
                continue

            if path.suffix not in extensions:
                continue

            try:
                lines = path.read_text(
                    encoding="utf-8"
                ).splitlines()

            except (UnicodeDecodeError, OSError):
                continue

            for line_number, line in enumerate(
                lines,
                start=1,
            ):
                if query.lower() in line.lower():
                    results.append(
                        {
                            "file": str(
                                path.relative_to(self.root)
                            ),
                            "line": line_number,
                            "text": line.strip(),
                        }
                    )

        return results
