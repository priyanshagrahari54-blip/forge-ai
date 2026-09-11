"""Structured agent packages (Forge Agent Creation Engine).

The agent factory turns a validated :class:`AgentSpec` into an
:class:`AgentPackage`: spec + semantic version + lifecycle state +
executor binding + test report + version history. The package is the
unit Forge stores, versions, benchmarks, and runs — plain data,
serializable to JSON, never code.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from forge.agents.lifecycle import LifecycleState, normalize
from forge.agents.spec import AgentSpec
from forge.agents.versioning import (INITIAL_VERSION, append_history,
                                     history_entry, validate)

#: Capability -> executor that really exists for it. ``real`` is True
#: only when the package binds one of these; anything else is an
#: honest specification that cannot run tasks.
CAPABILITY_EXECUTORS = {
    "coding": "coder",
    "debugging": "debugger",
    "testing": "tester",
    "review": "reviewer",
    "planning": "planner",
    "security": "security",
    "research": "researcher",
    "documentation": "coder",
    "reasoning": "planner",
}

MAX_PACKAGES = 40


@dataclass
class AgentPackage:
    spec: AgentSpec
    version: str = INITIAL_VERSION
    lifecycle: str = LifecycleState.CREATED.value
    created_by: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    executor: str = ""
    real: bool = False
    test_report: dict = field(default_factory=dict)
    version_history: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.spec, AgentSpec):
            raise ValueError("package spec must be an AgentSpec")
        self.version = validate(self.version)
        self.lifecycle = normalize(self.lifecycle)
        self.created_by = (self.created_by or "")[:64]
        if not isinstance(self.test_report, dict):
            raise ValueError("test_report must be an object")
        if not isinstance(self.version_history, list):
            raise ValueError("version_history must be a list")

    @property
    def name(self) -> str:
        return self.spec.name

    def manifest(self) -> dict[str, Any]:
        """The structured package: everything Forge needs to run it."""
        return {
            "name": self.spec.name,
            "version": self.version,
            "lifecycle": self.lifecycle,
            "executor": self.executor,
            "real": self.real,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "spec": self.spec.to_dict(),
            "wiring": {
                "model_fabric": True,
                "policy_gate": True,
                "tool_runtime": True,
                "memory": "namespaced:%s" % self.spec.name,
                "verification": True,
                "checkpoints": True,
            },
            "note": ("Backed by the registered %s executor."
                     % self.executor if self.real else
                     "A validated specification; no executor is bound "
                     "yet, so it cannot run tasks."),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec.to_dict(),
            "version": self.version,
            "lifecycle": self.lifecycle,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "executor": self.executor,
            "real": self.real,
            "test_report": dict(self.test_report),
            "version_history": list(self.version_history),
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "AgentPackage":
        if not isinstance(payload, dict):
            raise ValueError("An agent package must be a JSON object")
        spec = AgentSpec.from_dict(payload.get("spec"))
        package = cls(
            spec=spec,
            version=payload.get("version", INITIAL_VERSION),
            lifecycle=payload.get(
                "lifecycle", LifecycleState.CREATED.value),
            created_by=payload.get("created_by", ""),
            created_at=float(payload.get("created_at", time.time())),
            updated_at=float(payload.get("updated_at", time.time())),
            executor=payload.get("executor", ""),
            real=bool(payload.get("real", False)),
            test_report=dict(payload.get("test_report") or {}),
            version_history=list(
                payload.get("version_history") or []),
        )
        # Honesty on load: real requires a bound executor.
        if package.real and not package.executor:
            package.real = False
        return package


def resolve_executor(spec: AgentSpec) -> str:
    """Return the executor backing the spec's first known capability."""
    for capability in spec.capabilities:
        executor = CAPABILITY_EXECUTORS.get(capability)
        if executor:
            return executor
    return ""


def build_package(spec: AgentSpec, *, created_by: str = "",
                  bind: bool = False) -> AgentPackage:
    """Generate a structured agent package from a validated spec."""
    if not isinstance(spec, AgentSpec):
        raise ValueError("build_package needs an AgentSpec")
    executor = resolve_executor(spec) if bind else ""
    now = time.time()
    package = AgentPackage(
        spec=spec, version=INITIAL_VERSION,
        lifecycle=LifecycleState.CREATED.value,
        created_by=(created_by or "")[:64],
        created_at=now, updated_at=now,
        executor=executor, real=bool(executor),
        test_report={},
        version_history=[history_entry(
            INITIAL_VERSION, created_by or "",
            "created from specification")],
    )
    return package


def rebind(package: AgentPackage, *, bind: bool) -> AgentPackage:
    """Refresh the executor binding after a spec change."""
    executor = resolve_executor(package.spec) if bind else ""
    package.executor = executor
    package.real = bool(executor)
    package.updated_at = time.time()
    return package


def touch_history(package: AgentPackage, version: str, changed_by: str,
                  summary: str, *, kind: str = "patch") -> None:
    """Record a version bump in the package history (bounded)."""
    package.version = validate(version)
    package.version_history = append_history(
        package.version_history,
        history_entry(version, changed_by, summary, kind=kind))
    package.updated_at = time.time()
