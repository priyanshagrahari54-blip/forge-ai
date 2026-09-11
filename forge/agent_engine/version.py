"""Agent versioning (A81): semantic versions with change classification.

Versions are ``MAJOR.MINOR.PATCH``. The bump level is *derived* from
the specification diff rather than trusted from a caller:

* MAJOR — the permission, tool, or resource envelope widened, or a
  capability was removed. Anything that changes what an agent may do
  requires re-validation and re-testing.
* MINOR — capabilities/tools/gates changed without widening power.
* PATCH — cosmetic changes (purpose text, template label, author).

Widening is always MAJOR: an agent cannot quietly grow power in a
patch release.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MAJOR, MINOR, PATCH = "major", "minor", "patch"

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class VersionError(ValueError):
    """An invalid version string or an illegal version move."""


@dataclass(frozen=True)
class AgentVersion:
    major: int = 1
    minor: int = 0
    patch: int = 0

    def __str__(self) -> str:
        return "{0}.{1}.{2}".format(self.major, self.minor, self.patch)

    @classmethod
    def parse(cls, text: Any) -> "AgentVersion":
        if isinstance(text, AgentVersion):
            return text
        match = _VERSION_RE.match(str(text or "").strip())
        if not match:
            raise VersionError("Invalid version: {0!r}".format(text))
        return cls(int(match.group(1)), int(match.group(2)),
                   int(match.group(3)))

    def bump(self, level: str) -> "AgentVersion":
        level = (level or "").strip().lower()
        if level == MAJOR:
            return AgentVersion(self.major + 1, 0, 0)
        if level == MINOR:
            return AgentVersion(self.major, self.minor + 1, 0)
        if level == PATCH:
            return AgentVersion(self.major, self.minor, self.patch + 1)
        raise VersionError("Unknown bump level: {0!r}".format(level))

    def tuple(self) -> tuple:
        return (self.major, self.minor, self.patch)


def _widened(old: Any, new: Any) -> bool:
    """True when *new* contains anything *old* did not."""
    return bool(set(new or ()) - set(old or ()))


def classify_change(old_spec, new_spec) -> str:
    """Return the required bump level between two specifications."""
    if old_spec.name != new_spec.name:
        raise VersionError("A new version must keep the same agent name")

    old_perms = old_spec.permissions
    new_perms = new_spec.permissions

    widened_flags = (
        (new_perms.allow_network and not old_perms.allow_network)
        or (new_perms.allow_terminal and not old_perms.allow_terminal)
        or (new_perms.allow_git_commit and not old_perms.allow_git_commit)
        or (old_perms.require_approval_for_writes
            and not new_perms.require_approval_for_writes)
    )
    widened_scopes = (_widened(old_perms.read_paths, new_perms.read_paths)
                      or _widened(old_perms.write_paths,
                                  new_perms.write_paths)
                      or _widened(old_perms.domains, new_perms.domains))
    widened_tools = _widened(old_spec.tools, new_spec.tools)
    removed_capabilities = bool(set(old_spec.capabilities)
                                - set(new_spec.capabilities))

    old_limits = old_spec.resource_limits.to_dict()
    new_limits = new_spec.resource_limits.to_dict()
    raised_limits = any(new_limits[key] > old_limits[key]
                        for key in old_limits)

    weakened_gates = bool(set(old_spec.verification.required_gates)
                          - set(new_spec.verification.required_gates))
    weakened_verification = (
        weakened_gates
        or (old_spec.verification.require_checkpoint
            and not new_spec.verification.require_checkpoint)
        or (old_spec.verification.require_independent_review
            and not new_spec.verification.require_independent_review)
    )

    if (widened_flags or widened_scopes or widened_tools
            or removed_capabilities or raised_limits
            or weakened_verification):
        return MAJOR

    behavioral = (
        set(old_spec.capabilities) != set(new_spec.capabilities)
        or set(old_spec.tools) != set(new_spec.tools)
        or old_spec.model_requirements != new_spec.model_requirements
        or old_spec.memory_policy != new_spec.memory_policy
        or old_spec.verification != new_spec.verification
        or old_spec.permissions != new_spec.permissions
        or old_spec.resource_limits != new_spec.resource_limits
    )
    if behavioral:
        return MINOR
    return PATCH


def next_version(current, old_spec, new_spec) -> "AgentVersion":
    """Return the version a spec change must be released under."""
    return AgentVersion.parse(current).bump(
        classify_change(old_spec, new_spec))


class VersionHistory:
    """Ordered, append-only version history for one agent."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._entries: list = []

    def record(self, version, spec, *, level: str = "",
               note: str = "") -> dict:
        entry = {"version": str(AgentVersion.parse(version)),
                 "level": level, "note": note,
                 "spec_fingerprint": spec.fingerprint()}
        self._entries.append(entry)
        return dict(entry)

    def entries(self) -> list:
        return [dict(entry) for entry in self._entries]

    def latest(self) -> dict:
        return dict(self._entries[-1]) if self._entries else {}

    def __len__(self) -> int:
        return len(self._entries)
