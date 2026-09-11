"""Agent specifications (A81): the declarative contract for an agent.

An :class:`AgentSpec` is the *only* input to the agent factory. It is
strictly validated, immutable once frozen, and content-addressed, so a
package can always be traced back to the exact specification that
produced it.

A specification declares:

``name``                    stable identifier
``purpose``                 human-readable statement of intent
``capabilities``            canonical Model Fabric capabilities
``tools``                   tool names the agent may request
``permissions``             the permission envelope (never self-granted)
``model_requirements``      routing constraints for the Model Fabric
``memory_policy``           what the agent may remember and for how long
``verification``            gates that must pass before work is accepted
``resource_limits``         hard bounds on runs, wall clock, tokens, bytes

Nothing here executes anything. A specification is data; power is only
ever granted by an operator through the permission platform.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from forge.models.capabilities import is_capability

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,48}$")
TOOL_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,48}$")

MAX_PURPOSE = 500
MAX_CAPABILITIES = 12
MAX_TOOLS = 24
MAX_PATHS = 32

#: Tools an agent may never be given by a specification. These are
#: operator-only escalation surfaces; a spec that names one is refused.
FORBIDDEN_TOOLS = frozenset({
    "grant_permission", "grant", "escalate", "policy_write",
    "permission_write", "sudo", "become_root", "self_grant",
})

#: Memory scopes, from least to most durable.
MEMORY_SCOPES = ("none", "task", "session", "persistent")

#: Verification gates an agent can be required to pass.
VERIFICATION_GATES = ("tests", "build", "lint", "review", "security",
                      "benchmark")


class SpecError(ValueError):
    """A specification is invalid. Raised before anything is built."""


def _clean_name(value: Any, what: str) -> str:
    text = str(value or "").strip().lower()
    if not NAME_RE.match(text):
        raise SpecError(
            "{0} must match [a-z][a-z0-9_-]{{2,48}}: {1!r}".format(
                what, value))
    return text


def _unique(items: Iterable[Any]) -> tuple:
    seen = []
    for item in items:
        text = str(item or "").strip().lower()
        if text and text not in seen:
            seen.append(text)
    return tuple(seen)


def _check_paths(paths: Iterable[Any], what: str) -> tuple:
    cleaned = []
    for raw in paths:
        text = str(raw or "").strip()
        if not text:
            continue
        if text.startswith("/") or text.startswith("~") or "\\" in text:
            raise SpecError(
                "{0} scopes must be repository-relative: {1!r}".format(
                    what, raw))
        parts = text.split("/")
        if ".." in parts or ".git" in parts or ".forge" in parts:
            raise SpecError(
                "{0} scopes may not traverse or touch protected "
                "directories: {1!r}".format(what, raw))
        if text not in cleaned:
            cleaned.append(text)
    if len(cleaned) > MAX_PATHS:
        raise SpecError("Too many {0} scopes (max {1})".format(
            what, MAX_PATHS))
    return tuple(cleaned)


@dataclass(frozen=True)
class PermissionSpec:
    """The permission envelope an operator grants to an agent.

    The envelope is a *ceiling*, not a grant: it is intersected with the
    live permission platform, so it can only ever restrict what the
    platform already allows.
    """

    read_paths: tuple = ("**",)
    write_paths: tuple = ()
    allow_network: bool = False
    allow_terminal: bool = False
    allow_git_commit: bool = False
    #: Explicit approval is required for every write, regardless of mode.
    require_approval_for_writes: bool = True
    domains: tuple = ()

    @classmethod
    def from_dict(cls, data: Mapping) -> "PermissionSpec":
        data = dict(data or {})
        unknown = set(data) - {
            "read_paths", "write_paths", "allow_network", "allow_terminal",
            "allow_git_commit", "require_approval_for_writes", "domains"}
        if unknown:
            raise SpecError("Unknown permission fields: {0}".format(
                ", ".join(sorted(unknown))))
        read_paths = _check_paths(data.get("read_paths", ("**",)), "read")
        write_paths = _check_paths(data.get("write_paths", ()), "write")
        domains = _unique(data.get("domains", ()))
        for domain in domains:
            if "/" in domain or " " in domain:
                raise SpecError("Invalid domain: {0!r}".format(domain))
        allow_network = bool(data.get("allow_network", False))
        if domains and not allow_network:
            raise SpecError(
                "Domains were declared but allow_network is false")
        return cls(
            read_paths=read_paths,
            write_paths=write_paths,
            allow_network=allow_network,
            allow_terminal=bool(data.get("allow_terminal", False)),
            allow_git_commit=bool(data.get("allow_git_commit", False)),
            require_approval_for_writes=bool(
                data.get("require_approval_for_writes", True)),
            domains=domains,
        )

    def to_dict(self) -> dict:
        payload = asdict(self)
        for key in ("read_paths", "write_paths", "domains"):
            payload[key] = list(payload[key])
        return payload


@dataclass(frozen=True)
class ModelRequirements:
    """Routing constraints handed to the Model Fabric."""

    capability: str = "coding"
    min_context_window: int = 0
    max_output_tokens: int = 2048
    prefer_local: bool = True
    prefer_free: bool = True
    complexity: float = 1.0
    fallback_capability: str = ""

    @classmethod
    def from_dict(cls, data: Mapping) -> "ModelRequirements":
        data = dict(data or {})
        unknown = set(data) - {
            "capability", "min_context_window", "max_output_tokens",
            "prefer_local", "prefer_free", "complexity",
            "fallback_capability"}
        if unknown:
            raise SpecError("Unknown model requirement fields: {0}".format(
                ", ".join(sorted(unknown))))
        capability = str(data.get("capability", "coding")).strip().lower()
        if not is_capability(capability):
            raise SpecError(
                "Unknown model capability: {0!r}".format(capability))
        fallback = str(data.get("fallback_capability", "")).strip().lower()
        if fallback and not is_capability(fallback):
            raise SpecError(
                "Unknown fallback capability: {0!r}".format(fallback))
        try:
            window = int(data.get("min_context_window", 0))
            tokens = int(data.get("max_output_tokens", 2048))
            complexity = float(data.get("complexity", 1.0))
        except (TypeError, ValueError):
            raise SpecError("Model requirements must be numbers")
        if window < 0 or window > 2_000_000:
            raise SpecError("min_context_window out of range")
        if tokens < 1 or tokens > 200_000:
            raise SpecError("max_output_tokens out of range")
        if complexity <= 0 or complexity > 10:
            raise SpecError("complexity must be within (0, 10]")
        return cls(capability=capability, min_context_window=window,
                   max_output_tokens=tokens,
                   prefer_local=bool(data.get("prefer_local", True)),
                   prefer_free=bool(data.get("prefer_free", True)),
                   complexity=complexity, fallback_capability=fallback)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MemoryPolicy:
    """What an agent may remember, where, and for how long."""

    scope: str = "task"
    namespace: str = ""
    max_entries: int = 32
    max_bytes: int = 64 * 1024
    ttl_seconds: float = 3600.0
    allow_secrets: bool = False

    @classmethod
    def from_dict(cls, data: Mapping) -> "MemoryPolicy":
        data = dict(data or {})
        unknown = set(data) - {"scope", "namespace", "max_entries",
                               "max_bytes", "ttl_seconds", "allow_secrets"}
        if unknown:
            raise SpecError("Unknown memory policy fields: {0}".format(
                ", ".join(sorted(unknown))))
        scope = str(data.get("scope", "task")).strip().lower()
        if scope not in MEMORY_SCOPES:
            raise SpecError("Unknown memory scope: {0!r}".format(scope))
        if bool(data.get("allow_secrets", False)):
            # Storing secrets in agent memory is never permitted.
            raise SpecError("Agent memory may never be allowed to hold "
                            "secrets")
        try:
            entries = int(data.get("max_entries", 32))
            max_bytes = int(data.get("max_bytes", 64 * 1024))
            ttl = float(data.get("ttl_seconds", 3600.0))
        except (TypeError, ValueError):
            raise SpecError("Memory policy bounds must be numbers")
        if entries < 0 or entries > 10_000:
            raise SpecError("max_entries out of range")
        if max_bytes < 0 or max_bytes > 8 * 1024 * 1024:
            raise SpecError("max_bytes out of range")
        if ttl < 0 or ttl > 30 * 24 * 3600:
            raise SpecError("ttl_seconds out of range")
        namespace = str(data.get("namespace", "") or "").strip()
        if namespace:
            namespace = _clean_name(namespace, "memory namespace")
        return cls(scope=scope, namespace=namespace, max_entries=entries,
                   max_bytes=max_bytes, ttl_seconds=ttl,
                   allow_secrets=False)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class VerificationRequirements:
    """Gates that must pass before an agent's work can be accepted."""

    required_gates: tuple = ("tests", "security")
    require_checkpoint: bool = True
    require_independent_review: bool = True
    max_repair_attempts: int = 2

    @classmethod
    def from_dict(cls, data: Mapping) -> "VerificationRequirements":
        data = dict(data or {})
        unknown = set(data) - {"required_gates", "require_checkpoint",
                               "require_independent_review",
                               "max_repair_attempts"}
        if unknown:
            raise SpecError("Unknown verification fields: {0}".format(
                ", ".join(sorted(unknown))))
        gates = _unique(data.get("required_gates", ("tests", "security")))
        for gate in gates:
            if gate not in VERIFICATION_GATES:
                raise SpecError("Unknown verification gate: {0!r}".format(
                    gate))
        if "security" not in gates:
            # The security gate is mandatory for every agent.
            gates = tuple(gates) + ("security",)
        try:
            attempts = int(data.get("max_repair_attempts", 2))
        except (TypeError, ValueError):
            raise SpecError("max_repair_attempts must be an integer")
        if attempts < 0 or attempts > 10:
            raise SpecError("max_repair_attempts must be 0-10")
        return cls(required_gates=tuple(gates),
                   require_checkpoint=bool(
                       data.get("require_checkpoint", True)),
                   require_independent_review=bool(
                       data.get("require_independent_review", True)),
                   max_repair_attempts=attempts)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["required_gates"] = list(payload["required_gates"])
        return payload


@dataclass(frozen=True)
class ResourceLimits:
    """Hard, enforced bounds on what one agent may consume."""

    max_runs_per_hour: int = 30
    max_concurrent_runs: int = 1
    max_wall_seconds: float = 300.0
    max_tokens_per_run: int = 20_000
    max_files_touched: int = 20
    max_bytes_written: int = 1024 * 1024

    @classmethod
    def from_dict(cls, data: Mapping) -> "ResourceLimits":
        data = dict(data or {})
        fields = {"max_runs_per_hour", "max_concurrent_runs",
                  "max_wall_seconds", "max_tokens_per_run",
                  "max_files_touched", "max_bytes_written"}
        unknown = set(data) - fields
        if unknown:
            raise SpecError("Unknown resource limit fields: {0}".format(
                ", ".join(sorted(unknown))))
        defaults = cls()
        try:
            limits = cls(
                max_runs_per_hour=int(data.get(
                    "max_runs_per_hour", defaults.max_runs_per_hour)),
                max_concurrent_runs=int(data.get(
                    "max_concurrent_runs", defaults.max_concurrent_runs)),
                max_wall_seconds=float(data.get(
                    "max_wall_seconds", defaults.max_wall_seconds)),
                max_tokens_per_run=int(data.get(
                    "max_tokens_per_run", defaults.max_tokens_per_run)),
                max_files_touched=int(data.get(
                    "max_files_touched", defaults.max_files_touched)),
                max_bytes_written=int(data.get(
                    "max_bytes_written", defaults.max_bytes_written)),
            )
        except (TypeError, ValueError):
            raise SpecError("Resource limits must be numbers")
        bounds = (
            ("max_runs_per_hour", limits.max_runs_per_hour, 1, 1000),
            ("max_concurrent_runs", limits.max_concurrent_runs, 1, 16),
            ("max_wall_seconds", limits.max_wall_seconds, 1, 7200),
            ("max_tokens_per_run", limits.max_tokens_per_run, 1, 1_000_000),
            ("max_files_touched", limits.max_files_touched, 0, 500),
            ("max_bytes_written", limits.max_bytes_written, 0,
             64 * 1024 * 1024),
        )
        for name, value, low, high in bounds:
            if value < low or value > high:
                raise SpecError(
                    "{0} must be between {1} and {2}".format(
                        name, low, high))
        return limits

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AgentSpec:
    """A complete, validated agent specification."""

    name: str
    purpose: str
    capabilities: tuple = ()
    tools: tuple = ()
    permissions: PermissionSpec = field(default_factory=PermissionSpec)
    model_requirements: ModelRequirements = field(
        default_factory=ModelRequirements)
    memory_policy: MemoryPolicy = field(default_factory=MemoryPolicy)
    verification: VerificationRequirements = field(
        default_factory=VerificationRequirements)
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)
    template: str = ""
    created_by: str = ""

    # -- construction ---------------------------------------------------

    @classmethod
    def from_dict(cls, data: Mapping) -> "AgentSpec":
        if not isinstance(data, Mapping):
            raise SpecError("An agent specification must be a mapping")
        known = {"name", "purpose", "capabilities", "tools", "permissions",
                 "model_requirements", "memory_policy", "verification",
                 "verification_requirements", "resource_limits",
                 "template", "created_by"}
        unknown = set(data) - known
        if unknown:
            raise SpecError("Unknown specification fields: {0}".format(
                ", ".join(sorted(unknown))))

        name = _clean_name(data.get("name"), "Agent name")
        purpose = str(data.get("purpose", "") or "").strip()
        if not purpose:
            raise SpecError("An agent specification needs a purpose")
        if len(purpose) > MAX_PURPOSE:
            raise SpecError("purpose must be <= {0} characters".format(
                MAX_PURPOSE))

        capabilities = _unique(data.get("capabilities", ()))
        if not capabilities:
            raise SpecError("An agent needs at least one capability")
        if len(capabilities) > MAX_CAPABILITIES:
            raise SpecError("Too many capabilities (max {0})".format(
                MAX_CAPABILITIES))
        for capability in capabilities:
            if not is_capability(capability):
                raise SpecError(
                    "Capabilities must come from the canonical Model "
                    "Fabric vocabulary: {0!r}".format(capability))

        tools = _unique(data.get("tools", ()))
        if len(tools) > MAX_TOOLS:
            raise SpecError("Too many tools (max {0})".format(MAX_TOOLS))
        for tool in tools:
            if not TOOL_RE.match(tool):
                raise SpecError("Invalid tool name: {0!r}".format(tool))
            if tool in FORBIDDEN_TOOLS:
                raise SpecError(
                    "Tool {0!r} would allow permission self-grant and is "
                    "always refused".format(tool))

        permissions = PermissionSpec.from_dict(
            data.get("permissions") or {})
        model_requirements = ModelRequirements.from_dict(
            data.get("model_requirements") or {})
        memory_policy = MemoryPolicy.from_dict(
            data.get("memory_policy") or {})
        verification = VerificationRequirements.from_dict(
            data.get("verification")
            or data.get("verification_requirements") or {})
        resource_limits = ResourceLimits.from_dict(
            data.get("resource_limits") or {})

        if model_requirements.capability not in capabilities:
            raise SpecError(
                "The primary model capability {0!r} must also be declared "
                "in capabilities".format(model_requirements.capability))
        if permissions.write_paths and not tools:
            raise SpecError(
                "Write scopes were declared but no tools can perform "
                "writes")

        template = str(data.get("template", "") or "").strip().lower()
        created_by = str(data.get("created_by", "") or "").strip()[:64]

        return cls(name=name, purpose=purpose, capabilities=capabilities,
                   tools=tools, permissions=permissions,
                   model_requirements=model_requirements,
                   memory_policy=memory_policy, verification=verification,
                   resource_limits=resource_limits, template=template,
                   created_by=created_by)

    # -- serialization ---------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "capabilities": list(self.capabilities),
            "tools": list(self.tools),
            "permissions": self.permissions.to_dict(),
            "model_requirements": self.model_requirements.to_dict(),
            "memory_policy": self.memory_policy.to_dict(),
            "verification": self.verification.to_dict(),
            "resource_limits": self.resource_limits.to_dict(),
            "template": self.template,
            "created_by": self.created_by,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True,
                          separators=(",", ":"))

    def fingerprint(self) -> str:
        digest = hashlib.sha256(self.canonical_json().encode("utf-8"))
        return digest.hexdigest()[:16]

    def with_changes(self, **changes: Any) -> "AgentSpec":
        """Return a new validated spec with the given fields replaced."""
        payload = self.to_dict()
        payload.update(changes)
        return AgentSpec.from_dict(payload)
