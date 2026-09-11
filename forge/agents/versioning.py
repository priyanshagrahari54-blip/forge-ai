"""Agent versioning (Forge Agent Creation Engine).

Versions are strict ``major.minor.patch`` triples. Every spec change
bumps the version and appends a bounded history entry recording who
changed what and why — the audit trail of an agent's evolution.
"""
from __future__ import annotations

import re
import time
from typing import Any

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

INITIAL_VERSION = "1.0.0"
MAX_HISTORY = 50


def parse(version: str) -> tuple[int, int, int]:
    """Parse ``major.minor.patch`` or raise."""
    match = _VERSION.match((version or "").strip())
    if match is None:
        raise ValueError(
            "Version must look like major.minor.patch, got %r" % (version,))
    major, minor, patch = (int(part) for part in match.groups())
    if major > 999 or minor > 999 or patch > 9999:
        raise ValueError("Version components out of range: %r" % (version,))
    return major, minor, patch


def validate(version: str) -> str:
    """Return the normalized version or raise."""
    major, minor, patch = parse(version)
    return "%d.%d.%d" % (major, minor, patch)


def bump(version: str, kind: str = "patch") -> str:
    """Bump one component (``major``/``minor``/``patch``)."""
    major, minor, patch = parse(version)
    kind = (kind or "").strip().lower()
    if kind == "major":
        return "%d.0.0" % (major + 1,)
    if kind == "minor":
        return "%d.%d.0" % (major, minor + 1)
    if kind == "patch":
        return "%d.%d.%d" % (major, minor, patch + 1)
    raise ValueError(
        "Unknown bump kind %r; expected major, minor, or patch" % (kind,))


def history_entry(version: str, changed_by: str, summary: str,
                  *, kind: str = "patch") -> dict[str, Any]:
    """Build one bounded version-history entry."""
    return {
        "version": validate(version),
        "kind": (kind or "patch").strip().lower()[:16],
        "changed_by": (changed_by or "")[:64],
        "summary": (summary or "")[:280],
        "at": time.time(),
    }


def append_history(history: list, entry: dict) -> list:
    """Append an entry, keeping the history bounded (newest last)."""
    combined = list(history or []) + [dict(entry)]
    return combined[-MAX_HISTORY:]
