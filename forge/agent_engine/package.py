"""Agent packages (A81): the structured artifact built from a spec.

A package is a self-describing, content-addressed bundle:

``manifest``     identity, version, spec fingerprint, build metadata
``spec``         the exact validated specification
``runtime``      the wiring plan (fabric route, tool grants, memory
                 namespace, verification gates, checkpoint policy)
``prompt``       the deterministic system prompt derived from the spec
``state``        the lifecycle state

Packages never contain secrets, credentials, or executable code. They
describe what an agent may do; the runtime is what actually does it,
and every action still passes the live permission platform.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from forge.agent_engine.lifecycle import CREATED
from forge.agent_engine.spec import AgentSpec, SpecError
from forge.agent_engine.version import AgentVersion

PACKAGE_FORMAT = "forge-agent-package"
PACKAGE_FORMAT_VERSION = 1


@dataclass
class AgentPackage:
    """A built, structured agent package."""

    spec: AgentSpec
    version: AgentVersion
    runtime: dict = field(default_factory=dict)
    prompt: str = ""
    state: str = CREATED
    built_at: float = field(default_factory=time.time)
    built_by: str = ""
    benchmark: dict = field(default_factory=dict)
    validation: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def spec_fingerprint(self) -> str:
        return self.spec.fingerprint()

    def manifest(self) -> dict:
        return {
            "format": PACKAGE_FORMAT,
            "format_version": PACKAGE_FORMAT_VERSION,
            "name": self.spec.name,
            "version": str(self.version),
            "template": self.spec.template,
            "spec_fingerprint": self.spec_fingerprint,
            "package_id": self.package_id(),
            "built_at": self.built_at,
            "built_by": self.built_by,
            "state": self.state,
        }

    def package_id(self) -> str:
        payload = "{0}@{1}:{2}".format(
            self.spec.name, self.version, self.spec.fingerprint())
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "manifest": self.manifest(),
            "spec": self.spec.to_dict(),
            "runtime": dict(self.runtime),
            "prompt": self.prompt,
            "state": self.state,
            "benchmark": dict(self.benchmark),
            "validation": dict(self.validation),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, payload: Any) -> "AgentPackage":
        """Rebuild a package from an exported payload.

        Imported packages always come back **unbound and untrusted**:
        the state is reset to ``created`` and benchmark results are
        dropped, so importing can never smuggle in an enabled agent.
        """
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError as exc:
                raise SpecError("Malformed package JSON: {0}".format(exc))
        if not isinstance(payload, dict):
            raise SpecError("A package payload must be an object")
        manifest = payload.get("manifest") or {}
        if manifest.get("format") != PACKAGE_FORMAT:
            raise SpecError("Not a {0} payload".format(PACKAGE_FORMAT))
        if manifest.get("format_version") != PACKAGE_FORMAT_VERSION:
            raise SpecError("Unsupported package format_version: {0!r}"
                            .format(manifest.get("format_version")))
        spec = AgentSpec.from_dict(payload.get("spec") or {})
        version = AgentVersion.parse(manifest.get("version", "1.0.0"))
        declared = manifest.get("spec_fingerprint", "")
        if declared and declared != spec.fingerprint():
            raise SpecError(
                "Package fingerprint does not match its specification")
        from forge.agent_engine.factory import AgentFactoryEngine

        return AgentFactoryEngine().build(
            spec, version=version, built_by=str(
                manifest.get("built_by", ""))[:64])
