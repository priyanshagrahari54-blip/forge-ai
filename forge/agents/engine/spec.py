"""Agent Creation Engine (A81): structured agent specifications.

A specification is the *operator-authored contract* for a specialized
agent: who it is (name/purpose), what it can do (capabilities), what it
may touch (tools), what it may request (permissions — a ceiling, never
a grant), what models it needs, how it may remember (memory policy),
how its work is verified (verification requirements), and how much it
may consume (resource limits).

Everything validates against the canonical Forge vocabularies: model
capabilities come from ``forge.models.capabilities``, permission scopes
from the A33 ``RESOURCE_OPERATIONS`` table, and tool names from the
runtime tool registry vocabulary. A specification that does not
validate cannot enter the factory.

Specs are frozen: an agent can never edit its own contract at runtime.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from forge.models.capabilities import ALL_CAPABILITIES, is_capability
from forge.security.policy import RESOURCE_OPERATIONS, Resource

#: Agent names: lowercase slug, 3-49 chars (matches the A49 factory floor).
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{2,48}$")

MAX_PURPOSE = 500
MAX_CAPABILITIES = 12
MAX_TOOLS = 12
MAX_PERMISSIONS = 16

#: Canonical tool vocabulary: the names the permissioned ToolRuntime
#: understands (see ``forge.runtime.defaults.create_default_runtime``).
TOOL_VOCABULARY = (
    "read_file",
    "write_file",
    "delete_file",
    "search",
    "run_tests",
    "terminal",
    "git_status",
    "git_diff",
    "git_commit",
)

#: Tool name -> (A33 resource, operation) the tool exercises. Used to
#: bind the tool allowlist to the spec's permission ceiling.
TOOL_PERMISSION_MAP = {
    "read_file": (Resource.FILESYSTEM, "read"),
    "search": (Resource.FILESYSTEM, "read"),
    "write_file": (Resource.FILESYSTEM, "write"),
    "delete_file": (Resource.FILESYSTEM, "delete"),
    "terminal": (Resource.TERMINAL, "execute"),
    "run_tests": (Resource.TERMINAL, "execute"),
    "git_status": (Resource.GIT, "status"),
    "git_diff": (Resource.GIT, "diff"),
    "git_commit": (Resource.GIT, "commit"),
}

#: Routing presets the Model Fabric understands.
ROUTING_PRESETS = ("quality", "balanced", "fast", "free", "local", "privacy")

MEMORY_SCOPES = ("agent",)


def _clean_str_tuple(values: Any, *, label: str,
                     vocabulary: tuple[str, ...] | None = None,
                     max_items: int = 16) -> tuple[str, ...]:
    """Coerce to a deduplicated tuple of non-empty strings, validating
    each against ``vocabulary`` when provided."""
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{label} must be a list of strings")
    cleaned: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} entries must be non-empty strings")
        entry = value.strip()
        if vocabulary is not None and entry not in vocabulary:
            raise ValueError(
                f"Unknown {label} entry {entry!r}; choose from "
                f"{', '.join(vocabulary)}")
        if entry not in cleaned:
            cleaned.append(entry)
    if len(cleaned) > max_items:
        raise ValueError(f"{label} accepts at most {max_items} entries")
    return tuple(cleaned)


def _parse_permission(permission: str) -> tuple[Resource, str]:
    """Parse and validate a ``resource:operation`` permission scope."""
    text = (permission or "").strip()
    resource_name, sep, operation = text.partition(":")
    if not sep or not resource_name or not operation:
        raise ValueError(
            f"Permission scopes use 'resource:operation' (e.g. "
            f"'filesystem:read'); got {permission!r}")
    try:
        resource = Resource(resource_name.strip())
    except ValueError:
        raise ValueError(
            f"Unknown permission resource {resource_name!r}; choose from "
            f"{', '.join(item.value for item in Resource)}") from None
    operation = operation.strip()
    operations = RESOURCE_OPERATIONS[resource]
    if operation not in operations:
        raise ValueError(
            f"Unknown operation {operation!r} for resource "
            f"{resource.value}; choose from {', '.join(sorted(operations))}")
    return resource, operation


@dataclass(frozen=True)
class ModelRequirements:
    """What the agent needs from the Model Fabric.

    These are *requirements*, not grants: routing still passes through
    the fabric's own policy, and no requirement can ever select a model
    the fabric refuses.
    """

    capabilities: tuple[str, ...] = ()
    min_context_tokens: int = 0
    prefer_local: bool = True
    allow_remote: bool = True
    allow_paid: bool = False
    routing_policy: str = "balanced"

    def validate(self) -> None:
        _clean_str_tuple(self.capabilities, label="model capability",
                         vocabulary=ALL_CAPABILITIES, max_items=6)
        for capability in self.capabilities:
            if not is_capability(capability):
                raise ValueError(
                    f"Unknown model capability {capability!r}")
        if not isinstance(self.min_context_tokens, int) or \
                self.min_context_tokens < 0 or self.min_context_tokens > 2_000_000:
            raise ValueError("min_context_tokens must be 0-2000000")
        if self.routing_policy not in ROUTING_PRESETS:
            raise ValueError(
                f"Unknown routing policy {self.routing_policy!r}; choose "
                f"from {', '.join(ROUTING_PRESETS)}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "capabilities": list(self.capabilities),
            "min_context_tokens": self.min_context_tokens,
            "prefer_local": self.prefer_local,
            "allow_remote": self.allow_remote,
            "allow_paid": self.allow_paid,
            "routing_policy": self.routing_policy,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ModelRequirements":
        if isinstance(data, cls):
            return data
        data = data or {}
        return cls(
            capabilities=_clean_str_tuple(
                data.get("capabilities", ()),
                label="model capability", vocabulary=ALL_CAPABILITIES,
                max_items=6),
            min_context_tokens=int(data.get("min_context_tokens", 0)),
            prefer_local=bool(data.get("prefer_local", True)),
            allow_remote=bool(data.get("allow_remote", True)),
            allow_paid=bool(data.get("allow_paid", False)),
            routing_policy=str(data.get("routing_policy", "balanced")),
        )


@dataclass(frozen=True)
class MemoryPolicy:
    """How the agent may use Memory.

    ``scope='agent'`` gives the agent a private namespace rooted under
    ``.forge/agents/<name>/memory``; no other agent can reach it and it
    can reach no other agent's namespace. ``share_across_agents`` opts
    into the shared memory pool explicitly — it is off by default.
    """

    enabled: bool = True
    scope: str = "agent"
    max_entries: int = 100
    share_across_agents: bool = False

    def validate(self) -> None:
        if self.scope not in MEMORY_SCOPES:
            raise ValueError(
                f"Unknown memory scope {self.scope!r}; choose from "
                f"{', '.join(MEMORY_SCOPES)}")
        if not isinstance(self.max_entries, int) or \
                self.max_entries < 1 or self.max_entries > 1000:
            raise ValueError("max_entries must be 1-1000")

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "scope": self.scope,
            "max_entries": self.max_entries,
            "share_across_agents": self.share_across_agents,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "MemoryPolicy":
        if isinstance(data, cls):
            return data
        data = data or {}
        return cls(
            enabled=bool(data.get("enabled", True)),
            scope=str(data.get("scope", "agent")),
            max_entries=int(data.get("max_entries", 100)),
            share_across_agents=bool(data.get("share_across_agents", False)),
        )


@dataclass(frozen=True)
class VerificationRequirements:
    """Which verification gates the agent's work must pass."""

    run_tests: bool = True
    run_review: bool = True
    run_security: bool = True
    require_all: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_tests": self.run_tests,
            "run_review": self.run_review,
            "run_security": self.run_security,
            "require_all": self.require_all,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "VerificationRequirements":
        if isinstance(data, cls):
            return data
        data = data or {}
        return cls(
            run_tests=bool(data.get("run_tests", True)),
            run_review=bool(data.get("run_review", True)),
            run_security=bool(data.get("run_security", True)),
            require_all=bool(data.get("require_all", True)),
        )


@dataclass(frozen=True)
class ResourceLimits:
    """Hard consumption ceilings for the agent (enforced at dispatch)."""

    max_runs_per_hour: int = 30
    max_concurrent: int = 2
    max_tool_calls_per_run: int = 40
    max_output_tokens: int = 4096
    timeout_seconds: float = 600.0

    def validate(self) -> None:
        if not 1 <= self.max_runs_per_hour <= 1000:
            raise ValueError("max_runs_per_hour must be 1-1000")
        if not 1 <= self.max_concurrent <= 20:
            raise ValueError("max_concurrent must be 1-20")
        if not 1 <= self.max_tool_calls_per_run <= 500:
            raise ValueError("max_tool_calls_per_run must be 1-500")
        if not 64 <= self.max_output_tokens <= 32768:
            raise ValueError("max_output_tokens must be 64-32768")
        if not 5.0 <= self.timeout_seconds <= 7200.0:
            raise ValueError("timeout_seconds must be 5-7200")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_runs_per_hour": self.max_runs_per_hour,
            "max_concurrent": self.max_concurrent,
            "max_tool_calls_per_run": self.max_tool_calls_per_run,
            "max_output_tokens": self.max_output_tokens,
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ResourceLimits":
        if isinstance(data, cls):
            return data
        data = data or {}
        return cls(
            max_runs_per_hour=int(data.get("max_runs_per_hour", 30)),
            max_concurrent=int(data.get("max_concurrent", 2)),
            max_tool_calls_per_run=int(data.get("max_tool_calls_per_run", 40)),
            max_output_tokens=int(data.get("max_output_tokens", 4096)),
            timeout_seconds=float(data.get("timeout_seconds", 600.0)),
        )


@dataclass(frozen=True)
class AgentSpecification:
    """The complete, validated contract for one specialized agent."""

    name: str
    purpose: str
    capabilities: tuple[str, ...]
    tools: tuple[str, ...]
    permissions: tuple[str, ...]
    model_requirements: ModelRequirements = field(
        default_factory=ModelRequirements)
    memory_policy: MemoryPolicy = field(default_factory=MemoryPolicy)
    verification: VerificationRequirements = field(
        default_factory=VerificationRequirements)
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)
    template: str = ""
    version_note: str = ""

    def validate(self) -> None:
        if not self.name or not NAME_PATTERN.match(self.name):
            raise ValueError(
                "Agent name must match ^[a-z][a-z0-9_-]{2,48}$ "
                f"(got {self.name!r})")
        purpose = (self.purpose or "").strip()
        if not purpose:
            raise ValueError("Agent purpose must be non-empty")
        if len(purpose) > MAX_PURPOSE:
            raise ValueError(
                f"Agent purpose must be <= {MAX_PURPOSE} characters")
        _clean_str_tuple(self.capabilities, label="capability",
                         vocabulary=ALL_CAPABILITIES,
                         max_items=MAX_CAPABILITIES)
        if not self.capabilities:
            raise ValueError("An agent needs at least one capability")
        _clean_str_tuple(self.tools, label="tool",
                         vocabulary=TOOL_VOCABULARY, max_items=MAX_TOOLS)
        permissions = _clean_str_tuple(
            self.permissions, label="permission",
            max_items=MAX_PERMISSIONS)
        for permission in permissions:
            _parse_permission(permission)
        # Every tool must be covered by its mapped permission scope; a
        # tool without its permission is an unexecutable (and unsafe)
        # spec, so it is refused at the source.
        for tool in self.tools:
            resource, operation = TOOL_PERMISSION_MAP[tool]
            scope = f"{resource.value}:{operation}"
            if scope not in permissions:
                raise ValueError(
                    f"Tool {tool!r} requires permission {scope!r}; add it "
                    "or drop the tool")
        self.model_requirements.validate()
        self.memory_policy.validate()
        self.resource_limits.validate()
        if self.template and not NAME_PATTERN.match(self.template):
            raise ValueError(f"Invalid template name {self.template!r}")
        if len(self.version_note) > 200:
            raise ValueError("version_note must be <= 200 characters")

    @property
    def primary_capability(self) -> str:
        return self.capabilities[0] if self.capabilities else "reasoning"

    def permission_pairs(self) -> tuple[tuple[Resource, str], ...]:
        """The validated (resource, operation) permission ceiling."""
        pairs: list[tuple[Resource, str]] = []
        for permission in _clean_str_tuple(self.permissions,
                                           label="permission",
                                           max_items=MAX_PERMISSIONS):
            pairs.append(_parse_permission(permission))
        return tuple(pairs)

    def allows(self, resource: Resource | str, operation: str) -> bool:
        """True when (resource, operation) is inside the spec ceiling."""
        try:
            resource = Resource(resource)
        except ValueError:
            return False
        operation = (operation or "").lower()
        return any(pair == (resource, operation)
                   for pair in self.permission_pairs())

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "capabilities": list(self.capabilities),
            "tools": list(self.tools),
            "permissions": list(self.permissions),
            "model_requirements": self.model_requirements.to_dict(),
            "memory_policy": self.memory_policy.to_dict(),
            "verification": self.verification.to_dict(),
            "resource_limits": self.resource_limits.to_dict(),
            "template": self.template,
            "version_note": self.version_note,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentSpecification":
        if not isinstance(data, Mapping):
            raise ValueError("A specification must be a JSON object")
        spec = cls(
            name=str(data.get("name", "")),
            purpose=str(data.get("purpose", "")),
            capabilities=_clean_str_tuple(
                data.get("capabilities", ()), label="capability",
                vocabulary=ALL_CAPABILITIES, max_items=MAX_CAPABILITIES),
            tools=_clean_str_tuple(
                data.get("tools", ()), label="tool",
                vocabulary=TOOL_VOCABULARY, max_items=MAX_TOOLS),
            permissions=_clean_str_tuple(
                data.get("permissions", ()), label="permission",
                max_items=MAX_PERMISSIONS),
            model_requirements=ModelRequirements.from_dict(
                data.get("model_requirements")),
            memory_policy=MemoryPolicy.from_dict(data.get("memory_policy")),
            verification=VerificationRequirements.from_dict(
                data.get("verification")),
            resource_limits=ResourceLimits.from_dict(
                data.get("resource_limits")),
            template=str(data.get("template", "")),
            version_note=str(data.get("version_note", "")),
        )
        spec.validate()
        return spec


def validate_specification(spec: AgentSpecification) -> list[str]:
    """Validate a spec, returning a list of problems (empty = valid)."""
    try:
        spec.validate()
    except ValueError as exc:
        return [str(exc)]
    return []
