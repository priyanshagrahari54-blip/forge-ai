from pathlib import Path


class MemoryStore:
    def __init__(self, root: str = ".forge/memory") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, name: str, content: str) -> None:
        path = self.root / name
        path.write_text(content, encoding="utf-8")

    def load(self, name: str) -> str | None:
        path = self.root / name

        if not path.exists():
            return None

        return path.read_text(encoding="utf-8")
