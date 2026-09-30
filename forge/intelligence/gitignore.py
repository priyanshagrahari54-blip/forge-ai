from __future__ import annotations

from pathlib import Path, PurePath


class GitIgnoreMatcher:
    """Simple .gitignore-aware path matcher for Forge."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        # Performance optimization (Bolt ⚡): Use normalized string prefix with trailing slash for fast safe relative path derivation
        self.root_prefix = self.root.as_posix().rstrip("/") + "/"
        self.patterns: list[str] = []
        # Performance optimization (Bolt ⚡): Pre-compute pattern metadata tuples: (pattern, has_slash)
        self._parsed_patterns: list[tuple[str, bool]] = []
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

            pattern = line.rstrip("/")
            # Negated patterns are handled conservatively for now.
            if pattern.startswith("!"):
                continue

            has_slash = "/" in pattern
            self._parsed_patterns.append((pattern, has_slash))

    def is_ignored(self, path: str | Path) -> bool:
        """Return True if a repository path matches .gitignore."""
        path_key = str(path)
        if path_key in self._cache:
            return self._cache[path_key]

        # Performance optimization (Bolt ⚡): Fast relative path string calculation
        # Normalized as posix path string
        target_posix = Path(path).as_posix() if isinstance(path, Path) else path.replace("\\", "/")

        if target_posix.startswith(self.root_prefix):
            path_str = target_posix[len(self.root_prefix):]
            relative = PurePath(path_str)
        else:
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

        for pattern, has_slash in self._parsed_patterns:
            # Direct path match.
            if path_str == pattern:
                self._cache[path_key] = True
                return True

            # Match anything below a directory/pattern.
            if path_str.startswith(pattern + "/"):
                self._cache[path_key] = True
                return True

            # Simple filename / glob matching (passed as str for Python 3.8-3.11 compatibility)
            if relative.match(pattern):
                self._cache[path_key] = True
                return True

            # Pattern without a slash can match any path component.
            if not has_slash:
                if pattern in parts:
                    self._cache[path_key] = True
                    return True

        self._cache[path_key] = False
        return False
