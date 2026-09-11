"""The agent package (A81): the structured artifact the factory emits.

A package is the *whole agent* as data: identity (stable id), the
validated specification, the current version and its immutable
history, the lifecycle state, provenance (who created it), and the
subsystem bindings it operates through (Model Fabric, PolicyGate, Tool
Runtime, Memory, Verification, Checkpoints) expressed as *descriptors*
— names and policy flags, never live objects, so a package stays
serializable and portable.

The manifest format is ``forge-agent-package`` version 1.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from forge.agents.engine.lifecycle import LifecycleState
from forge.agents.engine.spec import AgentSpecification
from forge.agents.engine.versioning import VersionHistory

FORMAT = "forge-agent-package"
FORMAT_VERSION = 1

#: The subsystems every created agent operates through. Descriptors in
#: the manifest; the runtime binds them to real components at dispatch.
SUBSYSTEMS = (
    "model_fabric",
    "policy_gate",
    "tool_runtime",
    "memory",
    "verification",
    "checkpoints",
)


def _agent_id(name: str, spec_dict: dict) -> str:
    digest = hashlib.sha256(
        (name + "|" + repr(sorted(spec_dict.items()))).encode("utf-8")
    ).hexdigest()[:12]
    return f"agt_{digest}"


@dataclass
class AgentPackage:
    """A structured, versioned, lifecycle-tracked agent package."""

    spec: AgentSpecification
    agent_id: str
    version: int = 1
    status: str = LifecycleState.CREATED.value
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    created_by: str = "operator"
    history: VersionHistory = field(default=None)
    last_benchmark: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.history is None:
            self.history = VersionHistory(self.agent_id)

    # -- queries ----------------------------------------------------------

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def runnable(self) -> bool:
        return self.status == LifecycleState.ENABLED.value

    def touch(self) -> None:
        self.updated_at = time.time()

    # -- serialization ------------------------------------------------------

    def manifest(self) -> dict[str, Any]:
        """The full structured manifest (portable, no live objects)."""
        return {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "agent_id": self.agent_id,
            "name": self.spec.name,
            "purpose": self.spec.purpose,
            "template": self.spec.template,
            "version": self.version,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "created_by": self.created_by,
            "specification": self.spec.to_dict(),
            "subsystems": list(SUBSYSTEMS),
            "version_history": self.history.snapshot(),
            "last_benchmark": dict(self.last_benchmark),
        }

    def summary(self) -> dict[str, Any]:
        """The compact listing row."""
        return {
            "name": self.spec.name,
            "agent_id": self.agent_id,
            "version": self.version,
            "status": self.status,
            "template": self.spec.template,
            "capabilities": list(self.spec.capabilities),
            "tools": list(self.spec.tools),
            "purpose": self.spec.purpose[:160],
            "updated_at": self.updated_at,
            "created_by": self.created_by,
        }

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any]) -> "AgentPackage":
        if not isinstance(manifest, dict):
            raise ValueError("A package manifest must be a JSON object")
        if manifest.get("format") != FORMAT:
            raise ValueError(
                f"Not a {FORMAT} manifest (got {manifest.get('format')!r})")
        if manifest.get("format_version") != FORMAT_VERSION:
            raise ValueError(
                f"Unsupported package format_version "
                f"{manifest.get('format_version')!r}")
        spec = AgentSpecification.from_dict(
            manifest.get("specification", {}))
        agent_id = str(manifest.get("agent_id", ""))
        if not agent_id:
            raise ValueError("Package manifest is missing agent_id")
        history = VersionHistory.from_snapshot(
            agent_id, manifest.get("version_history", []))
        package = cls(
            spec=spec,
            agent_id=agent_id,
            version=int(manifest.get("version", 1)),
            status=str(manifest.get("status", LifecycleState.CREATED.value)),
            created_at=float(manifest.get("created_at", time.time())),
            updated_at=float(manifest.get("updated_at", time.time())),
            created_by=str(manifest.get("created_by", "operator")),
            history=history,
            last_benchmark=dict(manifest.get("last_benchmark", {})),
        )
        try:
            LifecycleState(package.status)
        except ValueError:
            raise ValueError(
                f"Unknown package status {package.status!r}") from None
        return package

    @classmethod
    def create(cls, spec: AgentSpecification, *, created_by: str,
               version: int = 1,
               status: str = LifecycleState.CREATED.value) -> "AgentPackage":
        """Build a fresh package for a validated spec."""
        spec.validate()
        spec_dict = spec.to_dict()
        package = cls(
            spec=spec,
            agent_id=_agent_id(spec.name, spec_dict),
            version=version,
            status=status,
            created_by=created_by,
        )
        package.history.append(spec_dict, changed_by=created_by,
                               note=spec.version_note or "created",
                               version=version)
        return package
