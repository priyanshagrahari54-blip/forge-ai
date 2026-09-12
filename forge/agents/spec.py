"""First-party Forge Agent Creation Engine: structured agent specifications.

An :class:`AgentSpec` is the single validated source of truth for creating
a specialized Forge agent. It carries exactly the nine required dimensions:

- ``name`` — unique agent identity (``[a-z][a-z0-9_-]{2,48}``)
- ``purpose`` — what the agent is for (1-1000 chars)
- ``capabilities`` — 1-12 names from the canonical Model Fabric vocabulary
- ``tools`` — 0-16 names from the closed tool vocabulary
- ``permissions`` — 0-16 explicit resource grants (resource/operation/scope)
- ``model_requirements`` — capability + routing hints for the Model Fabric
- ``memory_policy`` — per-agent memory bounds + mandatory isolation
- ``verification_requirements`` — gates that must pass before enablement
- ``resource_limits`` — runtime quotas enforced by the governor

Validation is strict and fail-closed: unknown capabilities, tools,
resources, operations, or gates are rejected — never silently dropped
(except for documented import-time skill dropping, which does not apply
here). A spec grants nothing by itself; requested permissions become
effective only through operator approval in the control plane.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from forge.models.capabilities import ALL_CAPABILITIES, is_capability
from forge.security.policy import RESOURCE_OPERATIONS, Resource

_NAME = re.compile(r"^[a-z][a-z0-9_-]{2,48}$")

MAX_PURPOSE = 1000
MAX_CAPABILITIES = 12
MAX_TOOLS = 16
MAX_PERMISSIONS = 16
MAX_MEMORY_ENTRIES = 500
MAX_MEMORY_VALUE = 10000
MAX_RUNS_PER_HOUR = 1000
MAX_CONCURRENT = 20
MAX_SECONDS = 3600

#: Closed tool vocabulary. Agents may only request these tools; the
#: managed executor allowlists execution to exactly this subset.
KNOWN_TOOLS: tuple[str, ...] = (
    "read_file",
    "write_file",
    "delete_file",
    "terminal",
    "run_tests",
    "search",
    "git_status",
    "git_diff",
    "git_commit",
    "browser",
    "network",
    "desktop",
    "memory_read",
    "memory_write",
    "vision",
    "voice",
)
KNOWN_TOOLS_SET = frozenset(KNOWN_TOOLS)

#: Verification gates an agent may require before enablement.
KNOWN_GATES: tuple[str, ...] = (
    "tests",
    "build",
    "lint",
    "security",
    "review",
)
KNOWN_GATES_SET = frozenset(KNOWN_GATES)

#: Permission effects a spec may request. ``DENY`` is not a grant and is
#: rejected here — denials live in operator policy, not agent specs.
KNOWN_EFFECTS = frozenset({"ALLOW", "REQUIRE_APPROVAL"})


def _dedup(items: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


@dataclass(frozen=True)
class PermissionGrant:
    """One explicit permission an agent requests.

    ``resource``/``operation`` must exist in the A33 engine vocabulary and
    ``scope`` must be a non-empty string (``""`` means the resource-default
    minimal scope, e.g. agent-scoped memory). Grants are requests only —
    they become effective solely through operator approval.
    """

    resource: str
    operation: str
    scope: str = ""
    effect: str = "REQUIRE_APPROVAL"

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "operation": self.operation,
            "scope": self.scope,
            "effect": self.effect,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PermissionGrant":
        if not isinstance(data, dict):
            raise ValueError("Each permission must be an object")
        resource = str(data.get("resource", "") or "").strip().lower()
        operation = str(data.get("operation", "") or "").strip().lower()
        scope = str(data.get("scope", "") or "")
        effect = str(data.get("effect", "REQUIRE_APPROVAL") or "").strip().upper()
        return cls(resource=resource, operation=operation, scope=scope,
                   effect=effect)

    def validate(self) -> None:
        try:
            resource = Resource(self.resource)
        except ValueError:
            raise ValueError(
                f"Unknown permission resource {self.resource!r}; "
                f"expected one of {sorted(r.value for r in Resource)}"
            ) from None
        if self.operation not in RESOURCE_OPERATIONS[resource]:
            raise ValueError(
                f"Unknown operation {self.operation!r} for resource "
                f"{resource.value!r}"
            )
        if not isinstance(self.scope, str) or len(self.scope) > 512:
            raise ValueError("Permission scope must be a string <= 512 chars")
        if self.effect not in KNOWN_EFFECTS:
            raise ValueError(
                f"Unknown permission effect {self.effect!r}; "
                "expected ALLOW or REQUIRE_APPROVAL"
            )
        # Terminal grants must pin a concrete executable scope — a blank
        # terminal scope would be an unbounded shell grant.
        if resource == Resource.TERMINAL and not self.scope.strip():
            raise ValueError(
                "Terminal permission grants must pin a concrete scope "
                "(executable); blank terminal scopes are rejected"
            )


@dataclass(frozen=True)
class ModelRequirements:
    capability: str = "coding"
    prefer_local: bool = True
    prefer_free: bool = True
    min_context: int = 0
    models: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "prefer_local": self.prefer_local,
            "prefer_free": self.prefer_free,
            "min_context": self.min_context,
            "models": list(self.models),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ModelRequirements":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ValueError("model_requirements must be an object")
        capability = str(data.get("capability", "coding") or "coding").strip().lower()
        models = data.get("models", ())
        if isinstance(models, str):
            models = (models,)
        if not isinstance(models, (list, tuple)):
            raise ValueError("model_requirements.models must be a list")
        return cls(
            capability=capability,
            prefer_local=bool(data.get("prefer_local", True)),
            prefer_free=bool(data.get("prefer_free", True)),
            min_context=int(data.get("min_context", 0) or 0),
            models=tuple(str(m)[:128] for m in models[:8]),
        )

    def validate(self) -> None:
        if not is_capability(self.capability):
            raise ValueError(
                f"Unknown model capability {self.capability!r}; "
                f"expected one of {list(ALL_CAPABILITIES)}"
            )
        if self.min_context < 0 or self.min_context > 1_000_000:
            raise ValueError("min_context must be 0-1000000")


@dataclass(frozen=True)
class MemoryPolicy:
    max_entries: int = 200
    max_value_chars: int = 2000
    isolated: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_entries": self.max_entries,
            "max_value_chars": self.max_value_chars,
            "isolated": self.isolated,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "MemoryPolicy":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ValueError("memory_policy must be an object")
        return cls(
            max_entries=int(data.get("max_entries", 200)),
            max_value_chars=int(data.get("max_value_chars", 2000)),
            isolated=bool(data.get("isolated", True)),
        )

    def validate(self) -> None:
        if self.max_entries < 1 or self.max_entries > MAX_MEMORY_ENTRIES:
            raise ValueError(
                f"max_entries must be 1-{MAX_MEMORY_ENTRIES}")
        if self.max_value_chars < 1 or self.max_value_chars > MAX_MEMORY_VALUE:
            raise ValueError(
                f"max_value_chars must be 1-{MAX_MEMORY_VALUE}")
        if self.isolated is not True:
            raise ValueError(
                "memory_policy.isolated must be true: per-agent memory "
                "isolation is mandatory and cannot be disabled")


@dataclass(frozen=True)
class ResourceLimits:
    max_runs_per_hour: int = 60
    max_concurrent: int = 2
    max_seconds: int = 300

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_runs_per_hour": self.max_runs_per_hour,
            "max_concurrent": self.max_concurrent,
            "max_seconds": self.max_seconds,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ResourceLimits":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ValueError("resource_limits must be an object")
        return cls(
            max_runs_per_hour=int(data.get("max_runs_per_hour", 60)),
            max_concurrent=int(data.get("max_concurrent", 2)),
            max_seconds=int(data.get("max_seconds", 300)),
        )

    def validate(self) -> None:
        if self.max_runs_per_hour < 1 or self.max_runs_per_hour > MAX_RUNS_PER_HOUR:
            raise ValueError(
                f"max_runs_per_hour must be 1-{MAX_RUNS_PER_HOUR}")
        if self.max_concurrent < 1 or self.max_concurrent > MAX_CONCURRENT:
            raise ValueError(f"max_concurrent must be 1-{MAX_CONCURRENT}")
        if self.max_seconds < 1 or self.max_seconds > MAX_SECONDS:
            raise ValueError(f"max_seconds must be 1-{MAX_SECONDS}")


@dataclass(frozen=True)
class AgentSpec:
    """Validated nine-dimension agent specification."""

    name: str
    purpose: str
    capabilities: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    permissions: tuple[PermissionGrant, ...] = ()
    model_requirements: ModelRequirements = field(
        default_factory=ModelRequirements)
    memory_policy: MemoryPolicy = field(default_factory=MemoryPolicy)
    verification_requirements: tuple[str, ...] = ("tests", "security")
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "capabilities": list(self.capabilities),
            "tools": list(self.tools),
            "permissions": [grant.to_dict() for grant in self.permissions],
            "model_requirements": self.model_requirements.to_dict(),
            "memory_policy": self.memory_policy.to_dict(),
            "verification_requirements": list(self.verification_requirements),
            "resource_limits": self.resource_limits.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentSpec":
        if not isinstance(data, dict):
            raise ValueError("An agent spec must be an object")
        name = str(data.get("name", "") or "").strip().lower()
        purpose = str(data.get("purpose", "") or "").strip()
        raw_caps = data.get("capabilities", ())
        if isinstance(raw_caps, str):
            raw_caps = (raw_caps,)
        capabilities = _dedup([
            str(cap or "").strip().lower()
            for cap in (raw_caps or ())])
        raw_tools = data.get("tools", ())
        if isinstance(raw_tools, str):
            raw_tools = (raw_tools,)
        tools = _dedup([
            str(tool or "").strip().lower()
            for tool in (raw_tools or ())])
        raw_permissions = data.get("permissions", ())
        if raw_permissions is None:
            raw_permissions = ()
        if not isinstance(raw_permissions, (list, tuple)):
            raise ValueError("permissions must be a list")
        permissions = tuple(
            PermissionGrant.from_dict(item) for item in raw_permissions)
        raw_gates = data.get("verification_requirements", ("tests", "security"))
        if isinstance(raw_gates, str):
            raw_gates = (raw_gates,)
        gates = _dedup([
            str(gate or "").strip().lower()
            for gate in (raw_gates or ())])
        return cls(
            name=name,
            purpose=purpose,
            capabilities=capabilities,
            tools=tools,
            permissions=permissions,
            model_requirements=ModelRequirements.from_dict(
                data.get("model_requirements")),
            memory_policy=MemoryPolicy.from_dict(data.get("memory_policy")),
            verification_requirements=gates,
            resource_limits=ResourceLimits.from_dict(
                data.get("resource_limits")),
        )

    def validate(self) -> None:
        if not _NAME.match(self.name):
            raise ValueError(
                "Agent names must match [a-z][a-z0-9_-]{2,48}")
        if not self.purpose or len(self.purpose) > MAX_PURPOSE:
            raise ValueError(
                f"purpose must be 1-{MAX_PURPOSE} characters")
        if not self.capabilities:
            raise ValueError("capabilities must list at least one capability")
        if len(self.capabilities) > MAX_CAPABILITIES:
            raise ValueError(f"Too many capabilities (max {MAX_CAPABILITIES})")
        for cap in self.capabilities:
            if not is_capability(cap):
                raise ValueError(
                    f"Unknown capability {cap!r}; expected one of "
                    f"{list(ALL_CAPABILITIES)}")
        if len(self.tools) > MAX_TOOLS:
            raise ValueError(f"Too many tools (max {MAX_TOOLS})")
        for tool in self.tools:
            if tool not in KNOWN_TOOLS_SET:
                raise ValueError(
                    f"Unknown tool {tool!r}; expected one of "
                    f"{list(KNOWN_TOOLS)}")
        if len(self.permissions) > MAX_PERMISSIONS:
            raise ValueError(f"Too many permissions (max {MAX_PERMISSIONS})")
        for grant in self.permissions:
            grant.validate()
        self.model_requirements.validate()
        self.memory_policy.validate()
        if not self.verification_requirements:
            raise ValueError(
                "verification_requirements must list at least one gate")
        for gate in self.verification_requirements:
            if gate not in KNOWN_GATES_SET:
                raise ValueError(
                    f"Unknown verification gate {gate!r}; expected one of "
                    f"{list(KNOWN_GATES)}")
        self.resource_limits.validate()


def validate_spec_dict(data: dict[str, Any]) -> AgentSpec:
    """Parse and validate a raw spec dict, returning the checked spec."""
    spec = AgentSpec.from_dict(data)
    spec.validate()
    return spec
