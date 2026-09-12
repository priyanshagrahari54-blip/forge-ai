"""Agent versioning (A81): immutable, semver-labelled spec snapshots.

A version record is evidence about what an agent *was*: the exact spec,
its fingerprint, who recorded it, and why. Records are never rewritten.

The bump rule is derived from the diff, not chosen by the caller, so a
change that widens power can never hide behind a patch number:

* **major** — capabilities, tools, or granted operations changed. These
  are the fields that decide what an agent may do.
* **minor** — model requirements, memory policy, verification
  requirements, or resource limits changed.
* **patch** — purpose, role, tags, or template provenance changed.
"""
from __future__ import annotations

import re
from typing import Any

from forge.agents.engine.errors import AgentVersionError
from forge.agents.engine.spec import AgentSpec

SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

MAJOR_FIELDS = ("capabilities", "tools", "permissions")
MINOR_FIELDS = ("model", "memory", "verification", "limits")
PATCH_FIELDS = ("purpose", "role", "tags", "template")


def parse_version(version: str) -> tuple:
    match = SEMVER.match((version or "").strip())
    if not match:
        raise AgentVersionError(
            "Version must look like MAJOR.MINOR.PATCH: %r" % version)
    return tuple(int(part) for part in match.groups())


def format_version(parts: tuple) -> str:
    return "%d.%d.%d" % (parts[0], parts[1], parts[2])


def bump(version: str, kind: str) -> str:
    """Return the next version for a ``major``/``minor``/``patch`` bump."""
    major, minor, patch = parse_version(version)
    if kind == "major":
        return format_version((major + 1, 0, 0))
    if kind == "minor":
        return format_version((major, minor + 1, 0))
    if kind == "patch":
        return format_version((major, minor, patch + 1))
    raise AgentVersionError("Unknown bump kind: %r" % kind)


def change_kind(old: AgentSpec, new: AgentSpec) -> str:
    """Classify a spec change as ``major``/``minor``/``patch``/``none``."""
    before = old.to_dict()
    after = new.to_dict()
    if any(before.get(key) != after.get(key) for key in MAJOR_FIELDS):
        return "major"
    if any(before.get(key) != after.get(key) for key in MINOR_FIELDS):
        return "minor"
    if any(before.get(key) != after.get(key) for key in PATCH_FIELDS):
        return "patch"
    if before.get("name") != after.get("name"):
        return "major"
    return "none"


def next_version(current: str, old: AgentSpec, new: AgentSpec) -> tuple:
    """Return ``(version, kind)`` for moving from ``old`` to ``new``."""
    kind = change_kind(old, new)
    if kind == "none":
        return current, "none"
    return bump(current, kind), kind


def diff_specs(old: AgentSpec, new: AgentSpec) -> dict:
    """Field-level diff used by the CLI and the desktop detail view."""
    before: dict = old.to_dict()
    after: dict = new.to_dict()
    changes: dict = {}
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changes[key] = {"before": before.get(key), "after": after.get(key)}
    return changes


def sort_versions(versions: Any) -> list:
    """Sort version labels newest-first (semver order, not string order)."""
    labels = [str(item) for item in versions]
    return sorted(labels, key=lambda label: parse_version(label), reverse=True)
