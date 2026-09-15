"""Profile registry and filesystem discovery (A83).

Precedence, highest first:

1. ``project`` profiles from ``<root>/.forge/profiles/*.yaml|json``
2. ``user`` profiles from ``~/.forge/profiles/*.yaml|json``
3. ``builtin`` profiles shipped in :mod:`forge.profiles.builtin`

A project profile may *replace* a builtin of the same name (that is how a
project customises a shipped profile) and may add new ones. Loading is
fail-honest: a malformed profile is recorded in ``errors`` and skipped rather
than silently accepted or silently dropped.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from forge.profiles.manifest import ProfileManifest, validate_profile

MAX_PROFILES = 32
MAX_PROFILE_BYTES = 512 * 1024
BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"
PROFILE_SUFFIXES = (".yaml", ".yml", ".json")


class ProfileError(ValueError):
    """Raised when a requested profile does not exist."""


def _parse_document(text: str, path: Path) -> Any:
    """Parse a profile document as YAML when available, else JSON."""
    try:
        import yaml  # type: ignore
    except Exception:
        if path.suffix.lower() == ".json":
            return json.loads(text)
        raise ValueError(
            "PyYAML is required to read %s (or use .json)" % path.name)
    return yaml.safe_load(text)


def load_profile_file(path: str | Path, *, origin: str = "project"
                      ) -> ProfileManifest:
    """Load and validate a single profile file."""
    file_path = Path(path)
    if not file_path.is_file():
        raise ProfileError("no such profile file: %s" % file_path)
    if file_path.stat().st_size > MAX_PROFILE_BYTES:
        raise ValueError("profile file exceeds %d bytes: %s"
                         % (MAX_PROFILE_BYTES, file_path.name))
    text = file_path.read_text(encoding="utf-8")
    payload = _parse_document(text, file_path)
    return validate_profile(payload, origin=origin, source=str(file_path))


def load_profile_dict(payload: Any, *, origin: str = "user",
                      source: str = "") -> ProfileManifest:
    """Validate an in-memory profile declaration."""
    return validate_profile(payload, origin=origin, source=source)


def discover_profile_files(*roots: Path) -> List[Path]:
    """Return profile files under the given roots, sorted for determinism."""
    found: List[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for candidate in sorted(root.iterdir()):
            if candidate.suffix.lower() in PROFILE_SUFFIXES and candidate.is_file():
                found.append(candidate)
    return found


class ProfileRegistry:
    """Validated project profiles, keyed by name."""

    #: Search order: the first origin to define a name wins.
    PRECEDENCE = ("project", "user", "builtin")

    def __init__(self) -> None:
        self._profiles: Dict[str, ProfileManifest] = {}
        self._errors: List[Dict[str, str]] = []

    # -- registration ----------------------------------------------------

    def register(self, manifest: ProfileManifest, *,
                 replace: bool = False) -> ProfileManifest:
        existing = self._profiles.get(manifest.name)
        if existing is not None and not replace:
            if existing.origin == manifest.origin:
                raise ProfileError(
                    "profile already registered: %s" % manifest.name)
            # A higher-precedence origin may replace a lower one.
            if (self.PRECEDENCE.index(manifest.origin)
                    >= self.PRECEDENCE.index(existing.origin)):
                raise ProfileError(
                    "profile %s from %s cannot replace the %s profile"
                    % (manifest.name, manifest.origin, existing.origin))
        self._profiles[manifest.name] = manifest
        return manifest

    def record_error(self, source: str, message: str) -> None:
        self._errors.append({"source": source, "error": message})
        # Bounded: a directory full of broken profiles must not grow forever.
        self._errors = self._errors[-MAX_PROFILES:]

    # -- lookup ----------------------------------------------------------

    def get(self, name: str) -> ProfileManifest:
        try:
            return self._profiles[(name or "").strip().lower()]
        except KeyError:
            raise ProfileError("unknown project profile: %s" % name) from None

    def has(self, name: str) -> bool:
        return (name or "").strip().lower() in self._profiles

    def list(self) -> List[ProfileManifest]:
        return [self._profiles[name] for name in sorted(self._profiles)]

    def errors(self) -> List[Dict[str, str]]:
        return list(self._errors)

    def __len__(self) -> int:
        return len(self._profiles)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.has(name)

    # -- matching --------------------------------------------------------

    def match(self, *, project_types: Tuple[str, ...] = (),
              languages: Tuple[str, ...] = ()) -> List[ProfileManifest]:
        """Rank profiles against observed project types and languages.

        Project types are the discriminator: a profile is only returned when
        the repository's detected project types overlap the profile's, so a
        plain Python service never picks up the operating-system profile just
        because both mention ``python``. Languages break ties (+2 per project
        type, +1 per language).

        When no project types were observed at all, language overlap alone is
        accepted — otherwise an empty repository could never match anything.
        """
        types = {value.strip().lower() for value in project_types if value}
        langs = {value.strip().lower() for value in languages if value}
        scored: List[Tuple[int, str, ProfileManifest]] = []
        for manifest in self.list():
            type_score = 2 * len(types & set(manifest.project_types))
            lang_score = len(langs & set(manifest.languages))
            if types:
                if not type_score:
                    continue
            elif not lang_score:
                continue
            scored.append((-(type_score + lang_score), manifest.name, manifest))
        scored.sort(key=lambda entry: (entry[0], entry[1]))
        return [entry[2] for entry in scored]

    def detect(self, root: str | Path) -> List[ProfileManifest]:
        """Match profiles against evidence found in a repository.

        Evidence is the file list of the repository root (bounded depth) plus
        the languages reported by :class:`RuntimeDetector` when it is
        available. Nothing is executed.
        """
        repo = Path(root)
        files = self._evidence_files(repo)
        languages: Tuple[str, ...] = ()
        try:
            from forge.intelligence.runtime_detection import RuntimeDetector
            languages = tuple(RuntimeDetector(repo).detect().project_type)
        except Exception:
            languages = ()
        project_types = self._project_types_from_evidence(files)
        return self.match(project_types=project_types, languages=languages)

    @staticmethod
    def _evidence_files(repo: Path, *, limit: int = 4000) -> List[str]:
        import os
        skip = {".git", ".venv", "venv", "node_modules", "__pycache__",
                ".pytest_cache", "build", "dist", ".forge"}
        out: List[str] = []
        for dirpath, dirnames, filenames in os.walk(str(repo)):
            dirnames[:] = [d for d in dirnames if d not in skip]
            for filename in filenames:
                full = Path(dirpath) / filename
                try:
                    out.append(str(full.relative_to(repo)).replace("\\", "/"))
                except ValueError:
                    continue
                if len(out) >= limit:
                    return out
        return out

    @staticmethod
    def _project_types_from_evidence(files: List[str]) -> Tuple[str, ...]:
        names = {Path(item).name.lower() for item in files}
        types: List[str] = []

        def note(value: str) -> None:
            if value not in types:
                types.append(value)

        if {"makefile", "cmakelists.txt"} & names or any(
                item.endswith((".c", ".cc", ".cpp", ".h", ".hpp"))
                for item in files):
            note("native")
        if "cargo.toml" in names or any(
                item.endswith(".rs") for item in files):
            note("rust")
        if "pyproject.toml" in names or "setup.py" in names or any(
                item.endswith(".py") for item in files):
            note("python")
        if "package.json" in names:
            note("node")
        if "build.gradle" in names or "build.gradle.kts" in names or any(
                item.endswith(".java") for item in files):
            note("jvm")
        if any(item.endswith((".asm", ".s", ".S")) for item in files):
            note("assembly")
        if any(item in ("kernel.ld", "linker.ld", "boot.asm", "boot.s",
                        "grub.cfg", "limine.conf") for item in names):
            note("operating-system")
            note("kernel")
        return tuple(types)

    def summary(self) -> Dict[str, Any]:
        return {
            "count": len(self._profiles),
            "profiles": [
                {"name": item.name, "version": item.version,
                 "origin": item.origin,
                 "project_types": list(item.project_types),
                 "recipes": len(item.recipes),
                 "knowledge": len(item.knowledge)}
                for item in self.list()
            ],
            "errors": list(self._errors),
        }


def default_registry(root: Optional[str | Path] = None, *,
                     include_user: bool = True) -> ProfileRegistry:
    """Build a registry: builtins, then user profiles, then project profiles.

    Files that fail validation are recorded as errors and skipped; a broken
    profile never prevents the rest from loading.
    """
    registry = ProfileRegistry()
    ordered: List[Tuple[str, Path]] = [
        ("builtin", path)
        for path in discover_profile_files(BUILTIN_DIR)
    ]
    if include_user:
        ordered.extend(
            ("user", path)
            for path in discover_profile_files(Path.home() / ".forge" / "profiles")
        )
    if root is not None:
        ordered.extend(
            ("project", path)
            for path in discover_profile_files(Path(root) / ".forge" / "profiles")
        )
    for origin, path in ordered[:MAX_PROFILES * 3]:
        try:
            registry.register(load_profile_file(path, origin=origin))
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            registry.record_error(str(path), str(exc))
    return registry
