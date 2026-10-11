from __future__ import annotations

from pathlib import Path


class GitIgnoreMatcher:
    """Simple .gitignore-aware path matcher for Forge."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.patterns: list[str] = []
        # Performance optimization (Bolt ⚡): Fast lookup structures for non-glob rules
        self.exact_matches: set[str] = set()
        self.prefix_matches: tuple[str, ...] = ()
        self.part_matches: set[str] = set()
        self.fallback_patterns: list[tuple[str, bool]] = []
        self._cache: dict[str, bool] = {}
        self._load()

    def _load(self) -> None:
        """Load patterns from the repository's .gitignore."""
        gitignore = self.root / ".gitignore"

        if not gitignore.exists():
            return

        prefixes: list[str] = []

        for line in gitignore.read_text(
            encoding="utf-8",
            errors="ignore",
        ).splitlines():
            line = line.strip()

            # Ignore empty lines and comments.
            if not line or line.startswith("#"):
                continue

            self.patterns.append(line)

            if line.startswith("!"):
                continue

            is_anchored = line.startswith("/") or ("/" in line.rstrip("/"))
            norm_pattern = line.strip("/")
            if not norm_pattern:
                continue

            has_glob = any(c in norm_pattern for c in "*?[")

            if not has_glob and not is_anchored:
                self.exact_matches.add(norm_pattern)
                prefixes.append(norm_pattern + "/")
                self.part_matches.add(norm_pattern)
            elif not has_glob and is_anchored:
                self.exact_matches.add(norm_pattern)
                prefixes.append(norm_pattern + "/")
            else:
                self.fallback_patterns.append((line.rstrip("/"), is_anchored))

        self.prefix_matches = tuple(prefixes)

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

        rel_str = relative.as_posix()
        parts = relative.parts

        # Fast path 1: Exact direct match or prefix match (O(1) set / prefix match)
        if rel_str in self.exact_matches:
            self._cache[path_key] = True
            return True

        if self.prefix_matches and rel_str.startswith(self.prefix_matches):
            self._cache[path_key] = True
            return True

        # Fast path 2: Component match for unanchored non-glob patterns (e.g. "node_modules", "__pycache__")
        if any(part in self.part_matches for part in parts):
            self._cache[path_key] = True
            return True

        # Fallback path for glob patterns: preserve exact Path.match and component matching semantics
        if self.fallback_patterns:
            for pattern, is_anchored in self.fallback_patterns:
                if relative.match(pattern):
                    self._cache[path_key] = True
                    return True
                if not is_anchored and pattern in parts:
                    self._cache[path_key] = True
                    return True

        self._cache[path_key] = False
        return False
