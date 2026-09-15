"""Project configuration: the file Forge ignored until A83.

``.forge/project.yaml`` has shipped in this repository since before A83 and
nothing read it (verified: ``grep -rn "project\\.yaml" --include=*.py`` had no
matches). This module makes it real: project name, languages, mode, testing,
git, security posture, selected profiles, and per-project overrides — all
validated, all defaulted, all read by the A83 engines.

Reading is side-effect free: a missing file yields defaults, a malformed file
raises ``ProjectConfigError`` with the reason rather than silently defaulting
(and silently pretending the user's settings applied).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from forge.profiles.registry import ProfileRegistry

CONFIG_CANDIDATES = (
    ".forge/project.yaml",
    ".forge/project.yml",
    ".forge/project.json",
)
MODES = ("development", "production", "research")
MAX_BYTES = 256 * 1024
MAX_NAME = 120


class ProjectConfigError(ValueError):
    """Raised for a malformed project configuration."""


def _text(value: Any, name: str, *, limit: int, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ProjectConfigError("%s must be a string" % name)
    text = value.strip()
    if len(text) > limit:
        raise ProjectConfigError("%s exceeds %d characters" % (name, limit))
    return text or default


def _flags(payload: Any, name: str) -> Dict[str, bool]:
    if payload in (None, {}):
        return {}
    if not isinstance(payload, dict):
        raise ProjectConfigError("%s must be a mapping" % name)
    out: Dict[str, bool] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            raise ProjectConfigError("%s keys must be strings" % name)
        if not isinstance(value, bool):
            raise ProjectConfigError("%s.%s must be true or false"
                                     % (name, key))
        out[key.strip().lower()] = value
    return out


def _list(value: Any, name: str, *, limit: int = 32) -> Tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ProjectConfigError("%s must be a list" % name)
    if len(value) > limit:
        raise ProjectConfigError("%s has too many entries (%d max)"
                                 % (name, limit))
    out: List[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ProjectConfigError("%s entries must be non-empty strings"
                                     % name)
        text = entry.strip()
        if text not in out:
            out.append(text)
    return tuple(out)


@dataclass(frozen=True)
class ProjectConfig:
    """Validated project configuration for one repository."""

    root: Path
    name: str = ""
    version: str = "0.0.0"
    languages: Tuple[str, ...] = ()
    mode: str = "development"
    #: Profiles explicitly selected by the project (empty == auto-detect).
    profiles: Tuple[str, ...] = ()
    testing: Dict[str, bool] = field(default_factory=lambda: {"enabled": True})
    git: Dict[str, bool] = field(default_factory=lambda: {"enabled": True})
    security: Dict[str, bool] = field(
        default_factory=lambda: {"require_approval_for_writes": True})
    #: Free-form engine overrides (``{"build": {"timeout_seconds": 900}}``).
    options: Dict[str, Any] = field(default_factory=dict)
    #: Where the config came from, or "" when defaults were used.
    source: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.source)

    @property
    def config_error(self) -> str:
        """Why the config was not applied, or "" when it was.

        Only ever set by ``load_project_config(..., strict=False)``: lenient
        callers get working defaults *and* the reason, so a report can state
        plainly that the user's settings were ignored.
        """
        return str(self.options.get("config_error", "") or "")

    @property
    def valid(self) -> bool:
        return not self.config_error

    def flag(self, section: str, key: str, default: bool = False) -> bool:
        return bool(self.section(section).get(key, default))

    def section(self, name: str) -> Dict[str, Any]:
        if name == "testing":
            return dict(self.testing)
        if name == "git":
            return dict(self.git)
        if name == "security":
            return dict(self.security)
        value = self.options.get(name)
        return dict(value) if isinstance(value, dict) else {}

    def option(self, section: str, key: str, default: Any = None) -> Any:
        return self.section(section).get(key, default)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "languages": list(self.languages),
            "mode": self.mode,
            "profiles": list(self.profiles),
            "testing": dict(self.testing),
            "git": dict(self.git),
            "security": dict(self.security),
            "options": {key: self.options[key] for key in sorted(self.options)},
            "source": self.source,
            "root": str(self.root),
        }


def find_config_file(root: str | Path) -> Optional[Path]:
    base = Path(root)
    for candidate in CONFIG_CANDIDATES:
        path = base / candidate
        if path.is_file():
            return path
    return None


def _parse(path: Path) -> Any:
    if path.stat().st_size > MAX_BYTES:
        raise ProjectConfigError("project config exceeds %d bytes" % MAX_BYTES)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ProjectConfigError("invalid JSON in %s: %s"
                                     % (path.name, exc)) from None
    try:
        import yaml  # type: ignore
    except Exception as exc:  # pragma: no cover - PyYAML is a hard dep
        raise ProjectConfigError(
            "PyYAML is required to read %s" % path.name) from exc
    try:
        return yaml.safe_load(text)
    except Exception as exc:
        raise ProjectConfigError("invalid YAML in %s: %s"
                                 % (path.name, exc)) from None


def load_project_config(root: str | Path = ".", *,
                        strict: bool = True) -> ProjectConfig:
    """Load ``.forge/project.yaml`` (or .yml/.json), falling back to defaults.

    ``strict=False`` downgrades a malformed config to defaults and records the
    reason in ``options["config_error"]`` — used by read-only reporting paths
    that must not fail. ``strict=True`` (the default) raises, because silently
    ignoring a user's configuration is exactly the bug this module fixes.
    """
    base = Path(root).resolve()
    path = find_config_file(base)
    if path is None:
        return ProjectConfig(root=base, name=base.name or "project")
    def fail(reason: str) -> ProjectConfig:
        return ProjectConfig(
            root=base, name=base.name or "project",
            options={"config_error": reason})

    try:
        payload = _parse(path)
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ProjectConfigError("project config must be a mapping")
        language = payload.get("language", payload.get("languages"))
        mode = _text(payload.get("mode"), "mode", limit=32,
                     default="development").lower()
        if mode not in MODES:
            raise ProjectConfigError(
                "mode must be one of %s" % (", ".join(MODES),))
        options = payload.get("options") or {}
        if not isinstance(options, dict):
            raise ProjectConfigError("options must be a mapping")
        known = {"name", "version", "language", "languages", "mode",
                 "profiles", "testing", "git", "security", "options"}
        unknown = sorted(set(payload) - known)
        if unknown:
            raise ProjectConfigError(
                "unknown project config keys: %s" % ", ".join(unknown))
    except ProjectConfigError as exc:
        if strict:
            raise
        # Lenient callers get defaults plus the reason, so a read-only
        # report still works and still says the config was not applied.
        return fail(str(exc))
    return ProjectConfig(
        root=base,
        name=_text(payload.get("name"), "name", limit=MAX_NAME,
                   default=base.name or "project"),
        version=_text(payload.get("version"), "version", limit=32,
                      default="0.0.0"),
        languages=_list(language, "language"),
        mode=mode,
        profiles=_list(payload.get("profiles"), "profiles"),
        testing={"enabled": True, **_flags(payload.get("testing"), "testing")},
        git={"enabled": True, **_flags(payload.get("git"), "git")},
        security={"require_approval_for_writes": True,
                  **_flags(payload.get("security"), "security")},
        options={key: options[key] for key in sorted(options)},
        source=str(path),
    )


def select_profiles(config: ProjectConfig,
                    registry: ProfileRegistry) -> Tuple[Any, ...]:
    """Resolve the profiles that apply to a configured project.

    Explicit ``profiles:`` entries win and must exist (a typo fails loudly
    instead of silently applying no domain knowledge). With no explicit list,
    profiles are matched against the repository's detected project types and
    languages.
    """
    if config.profiles:
        return tuple(registry.get(name) for name in config.profiles)
    return tuple(registry.detect(config.root))
