"""Durable project memory with bounded, path-safe storage.

Memory is project knowledge (facts, decisions, learned performance), never
ephemeral runtime state and never secrets. Keys are confined to the memory
root, entries are size-bounded, and callers can list/delete for retention.
"""
from __future__ import annotations

from pathlib import Path


class MemoryStore:
    DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB per entry

    def __init__(self, root: str = ".forge/memory", max_bytes: int | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes or self.DEFAULT_MAX_BYTES

    def _safe_path(self, name: str) -> Path:
        """Resolve a memory key to a path confined within the memory root.

        Rejects empty, absolute, and parent-traversing keys so callers cannot
        escape the memory directory.
        """
        if not name or not name.strip():
            raise ValueError("Memory key cannot be empty")
        candidate = Path(name)
        if candidate.is_absolute() or ".." in candidate.parts or "\\" in name:
            raise ValueError(f"Memory key must be a relative path without traversal: {name!r}")
        path = (self.root / candidate).resolve()
        try:
            path.relative_to(self.root.resolve())
        except ValueError:
            raise ValueError(f"Memory key escapes the memory directory: {name!r}") from None
        return path

    def save(self, name: str, content: str) -> None:
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise ValueError(
                f"Memory entry exceeds the {self.max_bytes}-byte size bound: {name!r}"
            )
        path = self._safe_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)

    def load(self, name: str) -> str | None:
        path = self._safe_path(name)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def exists(self, name: str) -> bool:
        return self._safe_path(name).exists()

    def delete(self, name: str) -> bool:
        path = self._safe_path(name)
        if not path.exists():
            return False
        path.unlink()
        return True

    def list(self) -> list[str]:
        """Return stored keys (relative POSIX paths) in deterministic order."""
        return sorted(
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file()
        )

    def clear(self) -> None:
        for path in sorted(self.root.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
