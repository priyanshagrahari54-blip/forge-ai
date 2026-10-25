from __future__ import annotations

import fnmatch
import re
from pathlib import Path


class GitIgnoreMatcher:
    """Simple .gitignore-aware path matcher for Forge."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.patterns: list[str] = []
        self._compiled_patterns: list[dict] = []
        self._cache: dict[str, bool] = {}
        self._load()

    def _load(self) -> None:
        """Load and precompile patterns from the repository's .gitignore."""
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

            # Negated patterns are handled conservatively for now.
            if line.startswith("!"):
                continue

            pattern = line.rstrip("/")
            if not pattern:
                continue

            # Precompile pattern characteristics for fast matching:
            # Avoid calling Path.match() repeatedly in is_ignored().
            has_slash = "/" in pattern
            has_glob = any(c in pattern for c in "*?[]")
            compiled_regex = (
                re.compile(fnmatch.translate(pattern)) if has_glob else None
            )

            self._compiled_patterns.append(
                {
                    "pattern": pattern,
                    "has_slash": has_slash,
                    "has_glob": has_glob,
                    "regex": compiled_regex,
                }
            )

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
        parts_set = set(parts)
        filename = parts[-1] if parts else ""

        # Performance Optimization:
        # Instead of iterating over string patterns and performing expensive
        # Path.match() calls on each check, use precompiled regexes and set lookups.
        for item in self._compiled_patterns:
            pattern = item["pattern"]
            has_slash = item["has_slash"]
            has_glob = item["has_glob"]
            regex = item["regex"]

            # Direct path match or match anything below a directory pattern.
            if path_str == pattern or path_str.startswith(pattern + "/"):
                self._cache[path_key] = True
                return True

            if has_glob:
                if has_slash:
                    if regex.match(path_str):
                        self._cache[path_key] = True
                        return True
                else:
                    if regex.match(filename) or regex.match(path_str):
                        self._cache[path_key] = True
                        return True
            else:
                # Pattern without a slash can match any path component (O(1) set lookup).
                if not has_slash:
                    if pattern in parts_set:
                        self._cache[path_key] = True
                        return True

        self._cache[path_key] = False
        return False
