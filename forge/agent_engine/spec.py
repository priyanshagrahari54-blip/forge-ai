"""Structured agent specification (A81).

An agent specification is the single, validated description of a
specialized agent:

- ``name``            — stable identity, ``[a-z][a-z0-9-]{2,48}``
- ``purpose``         — what the agent exists to do
- ``capabilities``    — canonical Model Fabric capability vocabulary
- ``tools``           — the exact tool surface the agent may call
- ``permissions``     — the exact permission operations granted (least
                        privilege: every tool's required operation must be
                        present and nothing else, unless an explicit
                        per-permission rationale is supplied)
- ``model``           — model requirements (minimum capabilities,
                        provider preference, context floor)
- ``memory``          — memory policy (enabled, bounds, retention)
- ``verification``    — verification requirements (tests, security
                        review, benchmark gate)
- ``limits``          — resource limits (tokens, requests, wall time,
                        files, working directories)

Validation is strict and fail-closed: every field is checked against a
bounded vocabulary, and any violation raises :class:`SpecError` listing
every problem, so a malformed spec can never silently become an agent.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

from forge.agent_engine.errors import SpecError
from forge.models.capabilities import is_capability

# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------

#: Tools an agent may declare, mapped to the permission operation each tool
#: requires. Every tool in the specification must have its required
#: permission granted; the agent is never given tools it cannot reach and
#: never given permissions no tool (or explicit rationale) justifies.
TOOL_PERMISSIONS: dict[str, str] = {
    "read_file": "read_file",
    "write_file": "write_file",
    "delete_file": "delete_file",
    "run_tests": "run_tests",
    "search": "search_files",
    "git_status": "git_status",
    "git_diff": "git_diff",
    "run_command": "run_command",
    "git_commit": "git_commit",
    "git_push": "git_push",
}

#: Permission operations an agent may ever be granted. ``delete_repository``
#: and ``expose_secrets`` are intentionally absent: they can never be
#: granted to any created agent.
KNOWN_PERMISSIONS: frozenset[str] = frozenset(TOOL_PERMISSIONS.values())

#: Operations that can never be granted, no matter the specification.
NEVER_GRANTABLE: frozenset[str] = frozenset(
    {"delete_repository", "expose_secrets"}
)

#: Read-only permission operations (SAFE level, no approval).
READ_PERMISSIONS: frozenset[str] = frozenset(
    {"read_file", "search_files", "run_tests", "git_status", "git_diff"}
)

#: Write permission operations (require explicit approval in assisted mode).
WRITE_PERMISSIONS: frozenset[str] = frozenset(
    {"write_file", "delete_file", "run_command", "git_commit", "git_push"}
)

#: Tool names whose calls modify the repository (counted as file writes).
MUTATING_TOOLS: frozenset[str] = frozenset(
    {"write_file", "delete_file", "run_command", "git_commit"}
)

PROVIDER_PREFERENCES: frozenset[str] = frozenset({"any", "local", "remote"})
MEMORY_RETENTIONS: frozenset[str] = frozenset({"version", "forever"})

_NAME = re.compile(r"^[a-z][a-z0-9-]{1,46}[a-z0-9]$")
_BENCHMARK_ID = re.compile(r"^[a-z][a-z0-9_-]{1,48}$")

MAX_PURPOSE_CHARS = 2000
MAX_CAPABILITIES = 16
MAX_TOOLS = 10
MAX_WORKING_DIRS = 12
MAX_MEMORY_ENTRIES = 10_000
MAX_MEMORY_ENTRY_BYTES = 1024 * 1024


def _is_safe_relative_path(path: str) -> bool:
    """Repo-relative path with no traversal, absolute, or backslash parts."""
    if not path or path.strip() != path:
        return False
    if path.startswith("/") or "\\" in path or path.startswith("~"):
        return False
    parts = [part for part in path.split("/") if part]
    if not parts or ".." in parts or any(part in (".", "..") for part in parts):
        return False
    if parts[0] in (".git", ".forge"):
        return False
    return True


# ---------------------------------------------------------------------------
# Sub-policies
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelRequirements:
    """Minimum model requirements the agent needs to operate.

    ``capabilities`` is the *floor*: routing must select a model that
    advertises every listed capability. ``preference`` constrains the
    provider kind (``any``/``local``/``remote``). ``min_context_tokens``
    is a context-window floor. ``allow_fallback`` records whether the
    deterministic offline fallback may satisfy this agent; when False,
    a fallback-only answer is reported as a failure, never as success.
    """

    capabilities: tuple[str, ...] = ("coding",)
    preference: str = "any"
    min_context_tokens: int = 0
    allow_fallback: bool = True


@dataclass(frozen=True)
class MemoryPolicy:
    """Memory policy: bounds, retention, and the engine-owned namespace.

    The agent never chooses its namespace — the runtime confines every
    entry under ``agents/<name>/``. ``retention`` decides whether memory
    is per-version (``version``) or shared across versions (``forever``).
    """

    enabled: bool = True
    max_entries: int = 500
    max_entry_bytes: int = 64 * 1024
    retention: str = "version"


@dataclass(frozen=True)
class VerificationRequirements:
    """What must pass before the agent may be enabled.

    ``benchmark`` names the benchmark suite that gates the ``TESTED``
    lifecycle state; ``min_score`` is the required pass fraction and
    ``min_scenarios`` the required number of passing scenarios.
    ``tests_required`` and ``security_review`` record additional
    verification gates the agent's work is subject to.
    """

    benchmark: str = "generic"
    min_score: float = 0.75
    min_scenarios: int = 0
    tests_required: bool = True
    security_review: bool = False


@dataclass(frozen=True)
class ResourceLimits:
    """Hard resource budgets enforced by the runtime, not advisory.

    ``working_dirs`` confines writes to the listed repo-relative
    directories; when empty, writes are confined to the repository as a
    whole (protected paths still deny via PolicyGate).
    """

    max_tokens_per_request: int = 8192
    max_requests: int = 200
    max_wall_seconds: float = 1800.0
    max_files_written: int = 50
    working_dirs: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# The specification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AgentSpec:
    """The complete, validated specification of one specialized agent."""

    name: str
    purpose: str
    capabilities: tuple[str, ...]
    tools: tuple[str, ...]
    permissions: tuple[str, ...]
    model: ModelRequirements = field(default_factory=ModelRequirements)
    memory: MemoryPolicy = field(default_factory=MemoryPolicy)
    verification: VerificationRequirements = field(
        default_factory=VerificationRequirements)
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    #: ``(permission, justification)`` pairs for permissions that no
    #: declared tool requires. Every extra grant must be explained.
    permission_rationale: tuple[tuple[str, str], ...] = ()

    # -- construction -----------------------------------------------------

    def __post_init__(self) -> None:
        # Normalize the tuple fields (dedupe, drop empties) so a spec is
        # canonical regardless of how it was constructed.
        object.__setattr__(
            self, "capabilities",
            tuple(dict.fromkeys(cap for cap in (self.capabilities or ())
                                if cap)))
        object.__setattr__(
            self, "tools",
            tuple(dict.fromkeys(tool for tool in (self.tools or ())
                                if tool)))
        object.__setattr__(
            self, "permissions",
            tuple(dict.fromkeys(permission for permission in
                                (self.permissions or ()) if permission)))
        issues = self.validate(raise_=False)
        if issues:
            raise SpecError("; ".join(issues))

    # -- validation -------------------------------------------------------

    def validate(self, *, raise_: bool = True) -> list[str]:
        """Validate the whole specification; return every issue found."""
        issues: list[str] = []

        if not _NAME.match(self.name or ""):
            issues.append(
                "name must match [a-z][a-z0-9-]{2,48}")
        purpose = (self.purpose or "").strip()
        if not purpose:
            issues.append("purpose is required")
        elif len(purpose) > MAX_PURPOSE_CHARS:
            issues.append(f"purpose exceeds {MAX_PURPOSE_CHARS} characters")

        caps = tuple(dict.fromkeys(self.capabilities or ()))
        if not caps:
            issues.append("at least one capability is required")
        elif any(not is_capability(cap) for cap in caps):
            issues.append(
                "capabilities must come from the canonical Model Fabric "
                "vocabulary")
        elif len(caps) > MAX_CAPABILITIES:
            issues.append(f"too many capabilities (max {MAX_CAPABILITIES})")

        tools = tuple(dict.fromkeys(self.tools or ()))
        if not tools:
            issues.append("at least one tool is required")
        elif any(tool not in TOOL_PERMISSIONS for tool in tools):
            issues.append(
                f"unknown tool; allowed tools: "
                f"{', '.join(sorted(TOOL_PERMISSIONS))}")
        elif len(tools) > MAX_TOOLS:
            issues.append(f"too many tools (max {MAX_TOOLS})")

        permissions = tuple(dict.fromkeys(self.permissions or ()))
        if not permissions:
            issues.append("at least one permission is required")
        else:
            for permission in permissions:
                if permission in NEVER_GRANTABLE:
                    issues.append(
                        f"permission {permission!r} can never be granted "
                        f"to a created agent")
                elif permission not in KNOWN_PERMISSIONS:
                    issues.append(
                        f"unknown permission {permission!r}; allowed: "
                        f"{', '.join(sorted(KNOWN_PERMISSIONS))}")
            implied = {TOOL_PERMISSIONS[tool] for tool in tools
                       if tool in TOOL_PERMISSIONS}
            missing = implied - set(permissions)
            if missing:
                issues.append(
                    "every declared tool's permission must be granted; "
                    f"missing: {', '.join(sorted(missing))}")
            extras = set(permissions) - implied
            rationale = dict(self.permission_rationale or ())
            for extra in sorted(extras):
                if not (rationale.get(extra) or "").strip():
                    issues.append(
                        f"permission {extra!r} is granted by no declared "
                        f"tool and has no rationale (least privilege)")
            for permission, _justification in self.permission_rationale:
                if permission not in extras:
                    issues.append(
                        f"rationale given for {permission!r} which is "
                        f"already implied by a declared tool")

        model_caps = tuple(dict.fromkeys(self.model.capabilities or ()))
        if model_caps and any(not is_capability(cap) for cap in model_caps):
            issues.append("model capabilities must use the canonical "
                          "vocabulary")
        if self.model.preference not in PROVIDER_PREFERENCES:
            issues.append(
                f"model preference must be one of "
                f"{', '.join(sorted(PROVIDER_PREFERENCES))}")
        if self.model.min_context_tokens < 0:
            issues.append("min_context_tokens cannot be negative")

        if self.memory.max_entries < 1 or self.memory.max_entries > MAX_MEMORY_ENTRIES:
            issues.append(
                f"memory.max_entries must be 1..{MAX_MEMORY_ENTRIES}")
        if (self.memory.max_entry_bytes < 1
                or self.memory.max_entry_bytes > MAX_MEMORY_ENTRY_BYTES):
            issues.append(
                f"memory.max_entry_bytes must be 1..{MAX_MEMORY_ENTRY_BYTES}")
        if self.memory.retention not in MEMORY_RETENTIONS:
            issues.append(
                f"memory.retention must be one of "
                f"{', '.join(sorted(MEMORY_RETENTIONS))}")

        if not _BENCHMARK_ID.match(self.verification.benchmark or ""):
            issues.append("verification.benchmark must look like a benchmark "
                          "id ([a-z][a-z0-9_-]{1,48})")
        if not 0.0 <= self.verification.min_score <= 1.0:
            issues.append("verification.min_score must be 0.0..1.0")
        if self.verification.min_scenarios < 0:
            issues.append("verification.min_scenarios cannot be negative")

        if self.limits.max_tokens_per_request < 1:
            issues.append("limits.max_tokens_per_request must be >= 1")
        if self.limits.max_requests < 1:
            issues.append("limits.max_requests must be >= 1")
        if self.limits.max_wall_seconds < 1:
            issues.append("limits.max_wall_seconds must be >= 1")
        if self.limits.max_files_written < 0:
            issues.append("limits.max_files_written cannot be negative")
        if len(self.limits.working_dirs) > MAX_WORKING_DIRS:
            issues.append(f"too many working_dirs (max {MAX_WORKING_DIRS})")
        for workdir in self.limits.working_dirs:
            if not _is_safe_relative_path(workdir):
                issues.append(
                    f"working_dir {workdir!r} must be a repo-relative "
                    f"path without traversal")

        if raise_ and issues:
            raise SpecError("; ".join(issues))
        return issues

    # -- derived data -----------------------------------------------------

    @property
    def implied_permissions(self) -> tuple[str, ...]:
        """Permissions required by the declared tools, in tool order."""
        return tuple(dict.fromkeys(
            TOOL_PERMISSIONS[tool] for tool in self.tools))

    def permission_digest(self) -> str:
        """Deterministic digest over the granted permission set only."""
        return hashlib.sha256(
            "\n".join(sorted(self.permissions)).encode("utf-8")
        ).hexdigest()

    def fingerprint(self) -> str:
        """Digest over the entire canonical specification."""
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True,
                       separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    # -- serialization ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose.strip(),
            "capabilities": list(self.capabilities),
            "tools": list(self.tools),
            "permissions": list(self.permissions),
            "model": {
                "capabilities": list(self.model.capabilities),
                "preference": self.model.preference,
                "min_context_tokens": self.model.min_context_tokens,
                "allow_fallback": self.model.allow_fallback,
            },
            "memory": {
                "enabled": self.memory.enabled,
                "max_entries": self.memory.max_entries,
                "max_entry_bytes": self.memory.max_entry_bytes,
                "retention": self.memory.retention,
            },
            "verification": {
                "benchmark": self.verification.benchmark,
                "min_score": self.verification.min_score,
                "min_scenarios": self.verification.min_scenarios,
                "tests_required": self.verification.tests_required,
                "security_review": self.verification.security_review,
            },
            "limits": {
                "max_tokens_per_request": self.limits.max_tokens_per_request,
                "max_requests": self.limits.max_requests,
                "max_wall_seconds": self.limits.max_wall_seconds,
                "max_files_written": self.limits.max_files_written,
                "working_dirs": list(self.limits.working_dirs),
            },
            "permission_rationale": [
                [permission, justification]
                for permission, justification in self.permission_rationale
            ],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AgentSpec":
        """Build a validated spec from a plain mapping (raises on issues)."""
        if not isinstance(payload, dict):
            raise SpecError("specification must be a JSON object")
        model = payload.get("model") or {}
        memory = payload.get("memory") or {}
        verification = payload.get("verification") or {}
        limits = payload.get("limits") or {}
        rationale_raw = payload.get("permission_rationale") or []

        def _int(mapping: dict, key: str, default: int) -> int:
            value = mapping.get(key)
            if value is None or value == "":
                return default
            try:
                return int(value)
            except (TypeError, ValueError):
                return int(float(value))

        def _float(mapping: dict, key: str, default: float) -> float:
            value = mapping.get(key)
            if value is None or value == "":
                return default
            return float(value)

        return cls(
            name=str(payload.get("name", "")),
            purpose=str(payload.get("purpose", "")),
            capabilities=tuple(str(cap) for cap in
                               payload.get("capabilities") or ()),
            tools=tuple(str(tool) for tool in payload.get("tools") or ()),
            permissions=tuple(str(permission) for permission in
                              payload.get("permissions") or ()),
            model=ModelRequirements(
                capabilities=tuple(str(cap) for cap in
                                   model.get("capabilities") or
                                   payload.get("capabilities") or ()),
                preference=str(model.get("preference", "any")),
                min_context_tokens=_int(model, "min_context_tokens", 0),
                allow_fallback=bool(model.get("allow_fallback", True)),
            ),
            memory=MemoryPolicy(
                enabled=bool(memory.get("enabled", True)),
                max_entries=_int(memory, "max_entries", 500),
                max_entry_bytes=_int(memory, "max_entry_bytes", 64 * 1024),
                retention=str(memory.get("retention", "version")),
            ),
            verification=VerificationRequirements(
                benchmark=str(verification.get("benchmark", "generic")),
                min_score=_float(verification, "min_score", 0.75),
                min_scenarios=_int(verification, "min_scenarios", 0),
                tests_required=bool(verification.get("tests_required",
                                                     True)),
                security_review=bool(verification.get("security_review",
                                                      False)),
            ),
            limits=ResourceLimits(
                max_tokens_per_request=_int(
                    limits, "max_tokens_per_request", 8192),
                max_requests=_int(limits, "max_requests", 200),
                max_wall_seconds=_float(limits, "max_wall_seconds", 1800.0),
                max_files_written=_int(limits, "max_files_written", 50),
                working_dirs=tuple(
                    str(path) for path in limits.get("working_dirs") or ()),
            ),
            permission_rationale=tuple(
                (str(permission), str(justification or ""))
                for permission, justification in rationale_raw
                if str(permission).strip()
            ),
        )


def with_permissions(spec: AgentSpec, permissions: tuple[str, ...],
                     *, rationale: tuple[tuple[str, str], ...] = ()) -> AgentSpec:
    """Return a copy of ``spec`` with a new permission set (and rationale).

    Does not validate escalation — that decision belongs to the factory,
    which applies the no-self-grant rule.
    """
    return replace(spec, permissions=tuple(dict.fromkeys(permissions)),
                   permission_rationale=tuple(rationale))
