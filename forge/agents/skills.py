"""Agent skills (A54): validated declarative skill definitions.

A skill is a named, versioned extension point: a capability from the
canonical vocabulary plus a bounded description. Skills are
declarative — attaching one updates the agent's capability set and
notes, never its executor, and grants nothing by itself. Execution
capabilities still come from the agent's bound executor and the A33
gates.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from forge.models.capabilities import is_capability

_NAME = re.compile(r"^[a-z][a-z0-9_-]{2,48}$")
MAX_DESCRIPTION = 500
MAX_SKILLS = 60
MAX_ATTACHED = 16


@dataclass
class SkillDefinition:
    name: str
    capability: str
    description: str = ""
    version: str = "1.0.0"
    created_by: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "capability": self.capability,
                "description": self.description, "version": self.version,
                "created_by": self.created_by,
                "created_at": self.created_at,
                "declarative": True,
                "note": "Declarative extension: attaching updates the "
                        "agent's capability set and notes; it never "
                        "changes the executor or grants power."}


class SkillRegistry:
    """Session-bounded validated skill definitions."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._skills: dict[str, SkillDefinition] = {}

    def create(self, name: str, capability: str, *, description: str = "",
               version: str = "1.0.0",
               created_by: str = "") -> SkillDefinition:
        name = (name or "").strip().lower()
        capability = (capability or "").strip().lower()
        if not _NAME.match(name):
            raise ValueError(
                "Skill names must match [a-z][a-z0-9_-]{2,48}")
        if not is_capability(capability):
            raise ValueError(
                "Skill capability must come from the canonical "
                "vocabulary")
        if len(self._skills) >= MAX_SKILLS:
            raise ValueError(f"skill limit reached ({MAX_SKILLS})")
        if name in self._skills:
            raise ValueError(f"Skill already defined: {name}")
        skill = SkillDefinition(
            name=name, capability=capability,
            description=(description or "").strip()[:MAX_DESCRIPTION],
            version=(version or "1.0.0")[:32],
            created_by=created_by or "")
        self._skills[name] = skill
        return skill

    def get(self, name: str) -> SkillDefinition | None:
        return self._skills.get((name or "").strip().lower())

    def list(self) -> list[SkillDefinition]:
        return [self._skills[name] for name in sorted(self._skills)]
