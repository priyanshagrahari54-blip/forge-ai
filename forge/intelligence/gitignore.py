from __future__ import annotations

from pathlib import Path


class GitIgnoreMatcher:
    """Simple .gitignore-aware path matcher for Forge."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.patterns: list[str] = []
        self._cache: dict[str, bool] = {}
        self._load()

    def _load(self) -> None:
        """Load patterns from the repository's .gitignore."""
        gitignore = self.root / ".gitignore"

        if not gitignore.exists():
            return

        for line in gitignore.read_text(
            encoding="utf-8",
            errors="ignore",
        ).splitlines():
            line = line.strip()

            # Ignore empty lines and comments.
            if not line or line.startswith("#"):
                continue

            self.patterns.append(line)

    def is_ignored(self, path: str | Path) -> bool:
        """Return True if a repository path matches .gitignore."""
        path_key = str(path)
        if path_key in self._cache:
            return self._cache[path_key]

        target = Path(path)

        if not target.is_absolute():
            target = self.root / target

        try:
            relative = target.relative_to(self.root)
        except ValueError:
            try:
                relative = target.resolve().relative_to(self.root)
            except ValueError:
                self._cache[path_key] = False
                return False

        path_str = relative.as_posix()
        parts = relative.parts

        for pattern in self.patterns:
            pattern = pattern.strip()

            if not pattern:
                continue

            # Negated patterns are handled conservatively for now.
            if pattern.startswith("!"):
                continue

            pattern = pattern.rstrip("/")

            # Direct path match.
            if path_str == pattern:
                self._cache[path_key] = True
                return True

            # Match anything below a directory/pattern.
            if path_str.startswith(pattern + "/"):
                self._cache[path_key] = True
                return True

            # Simple filename / glob matching.
            if relative.match(pattern):
                self._cache[path_key] = True
                return True

            # Pattern without a slash can match any path component.
            if "/" not in pattern:
                if pattern in parts:
                    self._cache[path_key] = True
                    return True

        self._cache[path_key] = False
        return False
