"""Structured agent packages with explicit versioning.

An :class:`AgentPackage` is what the factory generates from a validated
:class:`AgentSpec`: the spec snapshot, lifecycle state, semantic version,
version history, benchmark evidence, and provenance. Packages are plain
data — JSON-serializable, portable, and free of secrets, executors, and
live handles.

Versioning is strict semver (``MAJOR.MINOR.PATCH``):

- history preserves every prior ``(version, spec, actor, reason, at)`` entry;
- any spec change requires a version bump and resets lifecycle to
  ``created`` (re-validation and re-testing are mandatory);
- version strings must increase monotonically.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from forge.agents import lifecycle
from forge.agents.spec import AgentSpec, validate_spec_dict

_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
MAX_HISTORY = 50
MAX_REASON = 300

FORMAT = "forge-agent-package"
FORMAT_VERSION = 1


def parse_version(version: str) -> tuple[int, int, int]:
    match = _SEMVER.match((version or "").strip())
    if not match:
        raise ValueError(
            f"Invalid version {version!r}; expected MAJOR.MINOR.PATCH")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def compare_versions(left: str, right: str) -> int:
    """Return -1/0/1 comparing two semver strings."""
    parsed_left = parse_version(left)
    parsed_right = parse_version(right)
    if parsed_left < parsed_right:
        return -1
    if parsed_left > parsed_right:
        return 1
    return 0


def bump_version(version: str, part: str = "patch") -> str:
    major, minor, patch = parse_version(version)
    part = (part or "patch").strip().lower()
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(
        f"Unknown version part {part!r}; expected major, minor, or patch")


@dataclass
class AgentPackage:
    name: str
    spec: AgentSpec
    state: str = lifecycle.CREATED
    version: str = "1.0.0"
    created_by: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    history: list[dict[str, Any]] = field(default_factory=list)
    benchmarks: list[dict[str, Any]] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "name": self.name,
            "version": self.version,
            "state": self.state,
            "spec": self.spec.to_dict(),
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "history": [dict(entry) for entry in self.history],
            "benchmarks": [dict(entry) for entry in self.benchmarks],
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentPackage":
        if not isinstance(data, dict):
            raise ValueError("An agent package must be an object")
        if data.get("format") != FORMAT:
            raise ValueError("Not a forge-agent-package payload")
        if data.get("format_version") != FORMAT_VERSION:
            raise ValueError(
                f"Unsupported format_version {data.get('format_version')!r}")
        spec = validate_spec_dict(data.get("spec") or {})
        state = str(data.get("state", lifecycle.CREATED) or lifecycle.CREATED)
        if not lifecycle.is_state(state):
            raise ValueError(f"Unknown lifecycle state {state!r}")
        version = str(data.get("version", "1.0.0") or "1.0.0")
        parse_version(version)
        history = data.get("history", [])
        if not isinstance(history, list):
            raise ValueError("history must be a list")
        benchmarks = data.get("benchmarks", [])
        if not isinstance(benchmarks, list):
            raise ValueError("benchmarks must be a list")
        provenance = data.get("provenance", {})
        if not isinstance(provenance, dict):
            raise ValueError("provenance must be an object")
        package = cls(
            name=spec.name,
            spec=spec,
            state=state,
            version=version,
            created_by=str(data.get("created_by", "") or "")[:64],
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
            history=[dict(entry) for entry in history[:MAX_HISTORY]
                     if isinstance(entry, dict)],
            benchmarks=[dict(entry) for entry in benchmarks[-20:]
                         if isinstance(entry, dict)],
            provenance=dict(provenance),
        )
        if str(data.get("name", package.name) or package.name) != package.name:
            raise ValueError("Package name must match its spec name")
        return package

    def record_history(self, *, actor: str, reason: str) -> None:
        entry = {
            "version": self.version,
            "state": self.state,
            "spec": self.spec.to_dict(),
            "actor": (actor or "")[:64],
            "reason": (reason or "")[:MAX_REASON],
            "at": time.time(),
        }
        self.history.append(entry)
        self.history = self.history[-MAX_HISTORY:]

    def record_benchmark(self, report: dict[str, Any]) -> None:
        self.benchmarks.append(dict(report))
        self.benchmarks = self.benchmarks[-20:]
        self.updated_at = time.time()
