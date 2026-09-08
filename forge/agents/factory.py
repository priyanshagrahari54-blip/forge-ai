"""Agent creation (A49): validated, honest agent definitions.

A created agent is a *definition*: name, role, capabilities from the
canonical A31 vocabulary, and a description. ``real`` is True only
when the definition binds an executor that actually exists in the
agent registry for its role; otherwise it is honestly labeled as a
specification without a backing executor. Creating an agent never
grants capabilities — execution remains gated by the same A33
permission system as every built-in agent.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from forge.models.capabilities import is_capability

_NAME = re.compile(r"^[a-z][a-z0-9_-]{2,48}$")
_ROLE = re.compile(r"^[a-z][a-z0-9_-]{2,32}$")
MAX_DESCRIPTION = 500
MAX_CAPABILITIES = 12
MAX_AGENTS = 40

ROLE_EXECUTORS = {
    "coding": "coder",
    "debugging": "debugger",
    "testing": "tester",
    "review": "reviewer",
    "planning": "planner",
    "security": "security",
    "research": "researcher",
    "documentation": "coder",
}


@dataclass
class AgentDefinition:
    name: str
    role: str
    capabilities: tuple[str, ...]
    description: str = ""
    created_by: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    executor: str = ""
    real: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "capabilities": list(self.capabilities),
            "description": self.description,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "executor": self.executor,
            "real": self.real,
            "note": ("Backed by the registered {0} executor."
                     .format(self.executor) if self.real else
                     "A validated definition; no executor is bound "
                     "yet, so it cannot run tasks."),
        }


class AgentFactory:
    """Validated runtime agent definitions, session-bounded."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._definitions: dict[str, AgentDefinition] = {}

    def create(self, name: str, role: str, capabilities: tuple[str, ...]
               | list[str], *, description: str = "",
               created_by: str = "", bind: bool = False
               ) -> AgentDefinition:
        name = (name or "").strip().lower()
        role = (role or "").strip().lower()
        if not _NAME.match(name):
            raise ValueError(
                "Agent names must match [a-z][a-z0-9_-]{2,48}")
        if not _ROLE.match(role):
            raise ValueError("Roles must match [a-z][a-z0-9_-]{2,32}")
        caps = tuple(dict.fromkeys(
            (cap or "").strip().lower() for cap in capabilities))
        if not caps or any(not is_capability(cap) for cap in caps):
            raise ValueError(
                "Capabilities must come from the canonical vocabulary")
        if len(caps) > MAX_CAPABILITIES:
            raise ValueError("Too many capabilities")
        if len(self._definitions) >= MAX_AGENTS:
            raise ValueError(f"Agent limit reached ({MAX_AGENTS})")
        if name in self._definitions:
            raise ValueError(f"Agent already defined: {name}")
        description = (description or "").strip()[:MAX_DESCRIPTION]
        executor = ROLE_EXECUTORS.get(role, "") if bind else ""
        definition = AgentDefinition(
            name=name, role=role, capabilities=caps,
            description=description, created_by=created_by or "",
            executor=executor, real=bool(executor))
        self._definitions[name] = definition
        return definition

    def update(self, name: str, *, role: str = "",
               capabilities: tuple[str, ...] | list[str] | None = None,
               description: str | None = None) -> AgentDefinition:
        existing = self._definitions.get(name)
        if existing is None:
            raise ValueError(f"Unknown agent: {name}")
        role = (role or "").strip().lower() or existing.role
        if not _ROLE.match(role):
            raise ValueError("Roles must match [a-z][a-z0-9_-]{2,32}")
        if capabilities is not None:
            caps = tuple(dict.fromkeys(
                (cap or "").strip().lower() for cap in capabilities))
            if not caps or any(not is_capability(cap) for cap in caps):
                raise ValueError(
                    "Capabilities must come from the canonical vocabulary")
        else:
            caps = existing.capabilities
        executor = existing.executor
        if executor and role not in ROLE_EXECUTORS \
                or executor and ROLE_EXECUTORS.get(role) != executor:
            executor = ""  # role change invalidates the binding
        updated = AgentDefinition(
            name=name, role=role, capabilities=caps,
            description=(description if description is not None
                         else existing.description
                         ).strip()[:MAX_DESCRIPTION],
            created_by=existing.created_by,
            created_at=existing.created_at,
            updated_at=time.time(),
            executor=executor, real=bool(executor))
        self._definitions[name] = updated
        return updated

    def delete(self, name: str) -> AgentDefinition:
        existing = self._definitions.get(name)
        if existing is None:
            raise ValueError(f"Unknown agent: {name}")
        del self._definitions[name]
        return existing

    def get(self, name: str) -> AgentDefinition | None:
        return self._definitions.get(name)

    def list(self) -> list[AgentDefinition]:
        return [self._definitions[name] for name in sorted(self._definitions)]
