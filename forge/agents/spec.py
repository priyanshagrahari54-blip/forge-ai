"""Agent specification (Forge Agent Creation Engine).

An :class:`AgentSpec` is the single structured source of truth a new
specialized agent is created from::

    name, purpose, capabilities, tools, permissions,
    model requirements, memory policy,
    verification requirements, resource limits

Every field validates strictly at construction: unknown capabilities,
unknown tools, or permissions outside the allowlist raise instead of
loading partially. Blocked operations (``delete_repository``,
``expose_secrets``) can never appear in a spec — creation fails
closed.

Specs grant nothing by themselves. They become runnable only after
the engine validates, benchmarks, and enables the built package, and
every run still passes the Model Fabric, PolicyGate, Tool Runtime,
Memory, Verification, and Checkpoints.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from forge.models.capabilities import is_capability

_NAME = re.compile(r"^[a-z][a-z0-9_-]{2,48}$")

MAX_PURPOSE = 1000
MAX_CAPABILITIES = 12
MAX_TOOLS = 16
MAX_PERMISSIONS = 16

#: Tool names an agent may request. These map onto the permissioned
#: Tool Runtime; requesting a tool never bypasses its permission
#: level — unknown tools fail closed at spec validation.
KNOWN_TOOLS = frozenset({
    "read_file",
    "write_file",
    "delete_file",
    "terminal",
    "run_tests",
    "search",
    "git_status",
    "git_diff",
    "git_commit",
    "memory_read",
    "memory_write",
    "compute_execute",
    "model_generate",
})

#: A32 operations an agent may request. Blocked operations
#: (``delete_repository``, ``expose_secrets``) are deliberately
#: absent: no spec may name them.
KNOWN_PERMISSIONS = frozenset({
    "read_file",
    "search_files",
    "write_file",
    "delete_file",
    "run_command",
    "run_tests",
    "git_status",
    "git_diff",
    "git_commit",
    "git_push",
})

#: Operations that are always denied, named here so the error message
#: can explain *why* instead of just "unknown permission".
BLOCKED_OPERATIONS = frozenset({
    "delete_repository",
    "expose_secrets",
})


def _checked_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not _NAME.match(name):
        raise ValueError(
            "Agent names must match [a-z][a-z0-9_-]{2,48}")
    return name


def _deduped(items: Any, *, kind: str, limit: int) -> tuple:
    if not isinstance(items, (list, tuple)):
        raise ValueError("%s must be a list of strings" % kind)
    cleaned = tuple(dict.fromkeys(
        (item or "").strip().lower() for item in items))
    if not cleaned:
        raise ValueError("%s must list at least one entry" % kind)
    if any(not item for item in cleaned):
        raise ValueError("%s must not contain blank entries" % kind)
    if len(cleaned) > limit:
        raise ValueError(
            "%s lists too many entries (max %d)" % (kind, limit))
    return cleaned


@dataclass(frozen=True)
class ModelRequirements:
    """What the agent needs from the Model Fabric."""

    capabilities: tuple = ()
    prefer_local: bool = True
    prefer_free: bool = True
    min_context_window: int = 4096
    max_cost_per_token: float | None = None

    def __post_init__(self) -> None:
        caps = tuple(dict.fromkeys(
            (cap or "").strip().lower()
            for cap in self.capabilities))
        if not caps:
            raise ValueError(
                "model requirements must name at least one capability")
        if any(not is_capability(cap) for cap in caps):
            raise ValueError(
                "model capabilities must come from the canonical "
                "vocabulary")
        if len(caps) > MAX_CAPABILITIES:
            raise ValueError("Too many model capabilities")
        object.__setattr__(self, "capabilities", caps)
        if not isinstance(self.min_context_window, int) \
                or self.min_context_window < 512 \
                or self.min_context_window > 2000000:
            raise ValueError(
                "min_context_window must be an int in 512..2000000")
        if self.max_cost_per_token is not None and (
                not isinstance(self.max_cost_per_token, (int, float))
                or self.max_cost_per_token < 0):
            raise ValueError(
                "max_cost_per_token must be a non-negative number or null")

    def to_dict(self) -> dict[str, Any]:
        return {
            "capabilities": list(self.capabilities),
            "prefer_local": bool(self.prefer_local),
            "prefer_free": bool(self.prefer_free),
            "min_context_window": self.min_context_window,
            "max_cost_per_token": self.max_cost_per_token,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "ModelRequirements":
        if not isinstance(payload, dict):
            raise ValueError("model_requirements must be an object")
        return cls(
            capabilities=tuple(payload.get("capabilities") or ()),
            prefer_local=bool(payload.get("prefer_local", True)),
            prefer_free=bool(payload.get("prefer_free", True)),
            min_context_window=int(
                payload.get("min_context_window", 4096)),
            max_cost_per_token=payload.get("max_cost_per_token"),
        )


@dataclass(frozen=True)
class MemoryPolicy:
    """How much the agent may remember, and where."""

    max_entries: int = 200
    max_value_bytes: int = 2000
    allow_project_memory: bool = False
    retention_days: int = 30

    def __post_init__(self) -> None:
        if not isinstance(self.max_entries, int) \
                or not 1 <= self.max_entries <= 1000:
            raise ValueError("max_entries must be an int in 1..1000")
        if not isinstance(self.max_value_bytes, int) \
                or not 1 <= self.max_value_bytes <= 20000:
            raise ValueError(
                "max_value_bytes must be an int in 1..20000")
        if not isinstance(self.retention_days, int) \
                or not 1 <= self.retention_days <= 3650:
            raise ValueError(
                "retention_days must be an int in 1..3650")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_entries": self.max_entries,
            "max_value_bytes": self.max_value_bytes,
            "allow_project_memory": bool(self.allow_project_memory),
            "retention_days": self.retention_days,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "MemoryPolicy":
        if not isinstance(payload, dict):
            raise ValueError("memory_policy must be an object")
        return cls(
            max_entries=int(payload.get("max_entries", 200)),
            max_value_bytes=int(payload.get("max_value_bytes", 2000)),
            allow_project_memory=bool(
                payload.get("allow_project_memory", False)),
            retention_days=int(payload.get("retention_days", 30)),
        )


@dataclass(frozen=True)
class VerificationRequirements:
    """Which acceptance gates an agent must pass before enable."""

    require_tests: bool = True
    require_security: bool = True
    require_review: bool = True
    min_benchmark_pass_rate: float = 1.0

    def __post_init__(self) -> None:
        rate = self.min_benchmark_pass_rate
        if not isinstance(rate, (int, float)) or not 0.0 <= rate <= 1.0:
            raise ValueError(
                "min_benchmark_pass_rate must be a number in 0..1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "require_tests": bool(self.require_tests),
            "require_security": bool(self.require_security),
            "require_review": bool(self.require_review),
            "min_benchmark_pass_rate": float(
                self.min_benchmark_pass_rate),
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "VerificationRequirements":
        if not isinstance(payload, dict):
            raise ValueError("verification_requirements must be an object")
        return cls(
            require_tests=bool(payload.get("require_tests", True)),
            require_security=bool(payload.get("require_security", True)),
            require_review=bool(payload.get("require_review", True)),
            min_benchmark_pass_rate=float(
                payload.get("min_benchmark_pass_rate", 1.0)),
        )


@dataclass(frozen=True)
class ResourceLimits:
    """Runtime budgets enforced where the agent runs."""

    max_runs_per_hour: int = 60
    max_concurrent: int = 2
    max_seconds_per_run: float = 300.0
    max_output_chars: int = 20000
    max_files_per_run: int = 50

    def __post_init__(self) -> None:
        if not isinstance(self.max_runs_per_hour, int) \
                or not 1 <= self.max_runs_per_hour <= 1000:
            raise ValueError(
                "max_runs_per_hour must be an int in 1..1000")
        if not isinstance(self.max_concurrent, int) \
                or not 1 <= self.max_concurrent <= 20:
            raise ValueError("max_concurrent must be an int in 1..20")
        if not isinstance(self.max_seconds_per_run, (int, float)) \
                or not 1 <= self.max_seconds_per_run <= 3600:
            raise ValueError(
                "max_seconds_per_run must be a number in 1..3600")
        if not isinstance(self.max_output_chars, int) \
                or not 1 <= self.max_output_chars <= 200000:
            raise ValueError(
                "max_output_chars must be an int in 1..200000")
        if not isinstance(self.max_files_per_run, int) \
                or not 1 <= self.max_files_per_run <= 500:
            raise ValueError(
                "max_files_per_run must be an int in 1..500")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_runs_per_hour": self.max_runs_per_hour,
            "max_concurrent": self.max_concurrent,
            "max_seconds_per_run": float(self.max_seconds_per_run),
            "max_output_chars": self.max_output_chars,
            "max_files_per_run": self.max_files_per_run,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "ResourceLimits":
        if not isinstance(payload, dict):
            raise ValueError("resource_limits must be an object")
        return cls(
            max_runs_per_hour=int(
                payload.get("max_runs_per_hour", 60)),
            max_concurrent=int(payload.get("max_concurrent", 2)),
            max_seconds_per_run=float(
                payload.get("max_seconds_per_run", 300.0)),
            max_output_chars=int(payload.get("max_output_chars", 20000)),
            max_files_per_run=int(payload.get("max_files_per_run", 50)),
        )


@dataclass(frozen=True)
class AgentSpec:
    """Structured specification a new agent is created from."""

    name: str
    purpose: str
    capabilities: tuple = field(default_factory=tuple)
    tools: tuple = field(default_factory=tuple)
    permissions: tuple = field(default_factory=tuple)
    model_requirements: ModelRequirements = field(
        default_factory=lambda: ModelRequirements(("coding",)))
    memory_policy: MemoryPolicy = field(default_factory=MemoryPolicy)
    verification_requirements: VerificationRequirements = field(
        default_factory=VerificationRequirements)
    resource_limits: ResourceLimits = field(
        default_factory=ResourceLimits)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _checked_name(self.name))
        purpose = (self.purpose or "").strip()
        if not purpose:
            raise ValueError("purpose must be a non-empty string")
        if len(purpose) > MAX_PURPOSE:
            raise ValueError(
                "purpose must be at most %d characters" % MAX_PURPOSE)
        object.__setattr__(self, "purpose", purpose)
        caps = _deduped(self.capabilities, kind="capabilities",
                        limit=MAX_CAPABILITIES)
        if any(not is_capability(cap) for cap in caps):
            raise ValueError(
                "Capabilities must come from the canonical vocabulary")
        object.__setattr__(self, "capabilities", caps)
        tools = _deduped(self.tools, kind="tools", limit=MAX_TOOLS)
        unknown_tools = [tool for tool in tools
                         if tool not in KNOWN_TOOLS]
        if unknown_tools:
            raise ValueError(
                "Unknown tool(s): %s" % ", ".join(unknown_tools))
        object.__setattr__(self, "tools", tools)
        permissions = _deduped(self.permissions, kind="permissions",
                               limit=MAX_PERMISSIONS)
        blocked = [perm for perm in permissions
                   if perm in BLOCKED_OPERATIONS]
        if blocked:
            raise ValueError(
                "Blocked operation(s) can never be granted: %s"
                % ", ".join(blocked))
        unknown = [perm for perm in permissions
                   if perm not in KNOWN_PERMISSIONS]
        if unknown:
            raise ValueError(
                "Unknown permission(s): %s" % ", ".join(unknown))
        object.__setattr__(self, "permissions", permissions)
        if not isinstance(self.model_requirements, ModelRequirements):
            raise ValueError("model_requirements is malformed")
        if not isinstance(self.memory_policy, MemoryPolicy):
            raise ValueError("memory_policy is malformed")
        if not isinstance(self.verification_requirements,
                          VerificationRequirements):
            raise ValueError("verification_requirements is malformed")
        if not isinstance(self.resource_limits, ResourceLimits):
            raise ValueError("resource_limits is malformed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "capabilities": list(self.capabilities),
            "tools": list(self.tools),
            "permissions": list(self.permissions),
            "model_requirements": self.model_requirements.to_dict(),
            "memory_policy": self.memory_policy.to_dict(),
            "verification_requirements":
                self.verification_requirements.to_dict(),
            "resource_limits": self.resource_limits.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "AgentSpec":
        if not isinstance(payload, dict):
            raise ValueError("An agent spec must be a JSON object")
        model_raw = payload.get("model_requirements")
        memory_raw = payload.get("memory_policy")
        verification_raw = payload.get("verification_requirements")
        limits_raw = payload.get("resource_limits")
        return cls(
            name=payload.get("name", ""),
            purpose=payload.get("purpose", ""),
            capabilities=tuple(payload.get("capabilities") or ()),
            tools=tuple(payload.get("tools") or ()),
            permissions=tuple(payload.get("permissions") or ()),
            model_requirements=(
                ModelRequirements.from_dict(model_raw)
                if model_raw is not None
                else ModelRequirements(("coding",))),
            memory_policy=(
                MemoryPolicy.from_dict(memory_raw)
                if memory_raw is not None else MemoryPolicy()),
            verification_requirements=(
                VerificationRequirements.from_dict(verification_raw)
                if verification_raw is not None
                else VerificationRequirements()),
            resource_limits=(
                ResourceLimits.from_dict(limits_raw)
                if limits_raw is not None else ResourceLimits()),
        )
