"""First-party agent specifications for the Forge Agent Creation Engine.

An :class:`AgentSpec` is the structured, validated description Forge uses to
create a new specialized software agent. It carries exactly the nine
specification dimensions:

* name
* purpose
* capabilities (canonical Model Fabric vocabulary)
* tools (allowlist — every entry is genuinely executable: the seven real
  Tool Runtime tools plus ``memory_read``/``memory_write``, which the
  mediated runtime serves itself)
* permissions (requested grants — resource/operation pairs validated
  against the real A33 engine vocabulary; granted only by an
  operator/policy, never by the agent itself)
* model requirements (capability/context/cost/latency bounds)
* memory policy (namespaced, bounded, no cross-agent reads)
* verification requirements (tests/review/security/benchmark gates)
* resource limits (runs, concurrency, tool calls, wall clock)

Specs are pure data with strict validation. They grant nothing on their
own; the creation engine turns them into lifecycle-gated packages and the
mediated runtime enforces every boundary at execution time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from forge.models.capabilities import ALL_CAPABILITIES, is_capability
from forge.security.policy import (RESOURCE_OPERATIONS, Resource,
                                   validate_scope)

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,48}$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9_-]{2,32}$")

#: Envelope version for serialized agent specs. Unknown versions fail
#: closed at parse time so newer/foreign payloads can never slip through
#: an older validator.
SPEC_VERSION = "1.0"
SPEC_VERSION_RE = re.compile(r"^1\.0$")

MAX_PURPOSE = 500
MAX_CAPABILITIES = 12
MAX_TOOLS = 16
MAX_PERMISSIONS = 24
MAX_REASON = 280
MAX_SCOPE = 512

RISKS = frozenset({"NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"})
RETENTIONS = frozenset({"none", "session", "persistent"})

#: Tools a created agent may request. The first seven are the real tools
#: registered by ``forge.runtime.defaults.create_default_runtime``;
#: ``memory_read``/``memory_write`` are served by the mediated runtime
#: itself against the agent's isolated namespace. Anything else is
#: refused at validation time, and the mediated runtime enforces the
#: allowlist again at execution time.
KNOWN_TOOLS = frozenset({
    "read_file",
    "write_file",
    "delete_file",
    "terminal",
    "run_tests",
    "search",
    "git_status",
    "memory_read",
    "memory_write",
})

#: Tools the mediated runtime serves itself (never via the Tool Runtime).
MEDIATED_TOOLS = frozenset({"memory_read", "memory_write"})


def _issues_for_name(name: Any) -> list[str]:
    if not isinstance(name, str) or not NAME_RE.match(name.strip().lower()):
        return ["name must match [a-z][a-z0-9_-]{2,48}"]
    return []


def _strict_int(value: Any, field: str) -> int:
    """Parse an integer bound without silent coercion.

    Booleans, fractional numbers, and non-numeric strings are refused
    instead of being truncated or crashed on with a confusing error.
    """
    if isinstance(value, bool):
        raise ValueError("%s must be an integer, not a boolean" % field)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    raise ValueError("%s must be an integer" % field)


def _strict_float(value: Any, field: str) -> float:
    """Parse a float bound without silent coercion."""
    if isinstance(value, bool):
        raise ValueError("%s must be a number, not a boolean" % field)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            raise ValueError(
                "%s must be a number" % field) from None
    raise ValueError("%s must be a number" % field)


def _strict_str_list(value: Any, field: str) -> tuple[str, ...]:
    """Parse a string list; non-lists fail closed (never TypeError)."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError("%s must be a list of strings" % field)
    if any(not isinstance(entry, str) for entry in value):
        raise ValueError("%s must be a list of strings" % field)
    return tuple(entry.strip().lower() for entry in value)


def _not_bool_int(value: Any) -> bool:
    """True for genuine ints (booleans are not valid bounds)."""
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class PermissionRequestSpec:
    """One *requested* permission grant. Requests are inert until an
    operator (never the agent itself) grants them through the engine."""

    resource: str = ""
    operation: str = ""
    scope: str = ""
    risk: str = "NONE"
    reason: str = ""

    def validate(self) -> list[str]:
        issues: list[str] = []
        try:
            resource = Resource(self.resource)
        except ValueError:
            issues.append(
                "permission resource %r is unknown; expected one of %s"
                % (self.resource,
                   sorted(entry.value for entry in Resource)))
            return issues
        if self.operation not in RESOURCE_OPERATIONS[resource]:
            issues.append(
                "permission operation %r is unknown for resource %r; "
                "expected one of %s"
                % (self.operation, self.resource,
                   sorted(RESOURCE_OPERATIONS[resource])))
        if not isinstance(self.scope, str) or len(self.scope) > MAX_SCOPE:
            issues.append("permission scope must be a string of at most "
                          "%d characters (empty means the resource-default "
                          "minimal scope)" % MAX_SCOPE)
        # A blank terminal scope would be an unbounded shell grant — pin
        # a concrete executable instead.
        if resource == Resource.TERMINAL and not self.scope.strip():
            issues.append("terminal permission requests must pin a concrete "
                          "scope (executable); blank terminal scopes are "
                          "rejected")
        if resource == Resource.TERMINAL and self.scope.strip() in (
                "*", "**"):
            issues.append("terminal permission scopes must pin a concrete "
                          "executable; wildcard-only scopes are rejected")
        if resource == Resource.FILESYSTEM and isinstance(self.scope, str) \
                and len(self.scope) <= MAX_SCOPE:
            try:
                validate_scope(Resource.FILESYSTEM, self.scope)
            except ValueError as exc:
                issues.append("permission scope %r is invalid: %s"
                              % (self.scope, exc))
        if self.risk not in RISKS:
            issues.append("permission risk %r must be one of %s"
                          % (self.risk, sorted(RISKS)))
        if not isinstance(self.reason, str) or not self.reason.strip() \
                or len(self.reason) > MAX_REASON:
            issues.append("permission reason must be 1-%d characters"
                          % MAX_REASON)
        return issues

    def to_dict(self) -> dict[str, Any]:
        return {"resource": self.resource, "operation": self.operation,
                "scope": self.scope, "risk": self.risk,
                "reason": self.reason}

    @classmethod
    def from_dict(cls, payload: Any) -> "PermissionRequestSpec":
        if not isinstance(payload, dict):
            raise ValueError("permission entries must be objects")
        scope = payload.get("scope", "")
        if scope is None:
            scope = ""
        if not isinstance(scope, str):
            raise ValueError("permission scope must be a string")
        return cls(
            resource=str(payload.get("resource", "")).strip().lower(),
            operation=str(payload.get("operation", "")).strip().lower(),
            scope=scope,
            risk=str(payload.get("risk", "NONE")).strip().upper(),
            reason=str(payload.get("reason", "")).strip(),
        )


@dataclass(frozen=True)
class ModelRequirements:
    capabilities: tuple[str, ...] = ("coding",)
    min_context_window: int = 0
    prefer_local: bool = False
    prefer_free: bool = False
    max_cost_per_token: float | None = None
    max_latency_ms: float | None = None

    def validate(self) -> list[str]:
        issues: list[str] = []
        if not self.capabilities or len(self.capabilities) > 6:
            issues.append("model_requirements.capabilities must hold 1-6 "
                          "entries")
        elif any(not is_capability(cap) for cap in self.capabilities):
            issues.append("model_requirements.capabilities must come from "
                          "the canonical vocabulary: %s"
                          % (", ".join(ALL_CAPABILITIES),))
        if not _not_bool_int(self.min_context_window) \
                or self.min_context_window < 0 \
                or self.min_context_window > 1000000:
            issues.append("model_requirements.min_context_window must be "
                          "an integer 0-1000000")
        for label, value in (("max_cost_per_token",
                              self.max_cost_per_token),
                             ("max_latency_ms", self.max_latency_ms)):
            if value is not None and (
                    not isinstance(value, (int, float))
                    or not (value >= 0)):
                issues.append("model_requirements.%s must be a "
                              "non-negative number or null" % label)
        return issues

    def to_dict(self) -> dict[str, Any]:
        return {"capabilities": list(self.capabilities),
                "min_context_window": self.min_context_window,
                "prefer_local": self.prefer_local,
                "prefer_free": self.prefer_free,
                "max_cost_per_token": self.max_cost_per_token,
                "max_latency_ms": self.max_latency_ms}

    @classmethod
    def from_dict(cls, payload: Any) -> "ModelRequirements":
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValueError("model_requirements must be an object")
        raw_cost = payload.get("max_cost_per_token")
        raw_latency = payload.get("max_latency_ms")
        return cls(
            capabilities=_strict_str_list(
                payload.get("capabilities", ["coding"]),
                "model_requirements.capabilities"),
            min_context_window=_strict_int(
                payload.get("min_context_window", 0),
                "model_requirements.min_context_window"),
            prefer_local=bool(payload.get("prefer_local", False)),
            prefer_free=bool(payload.get("prefer_free", False)),
            max_cost_per_token=(
                None if raw_cost is None else _strict_float(
                    raw_cost, "model_requirements.max_cost_per_token")),
            max_latency_ms=(
                None if raw_latency is None else _strict_float(
                    raw_latency, "model_requirements.max_latency_ms")))


@dataclass(frozen=True)
class MemoryPolicy:
    retention: str = "session"
    max_entries: int = 200
    max_bytes_per_entry: int = 20480
    allow_cross_agent_read: bool = False

    def validate(self) -> list[str]:
        issues: list[str] = []
        if self.retention not in RETENTIONS:
            issues.append("memory_policy.retention must be one of %s"
                          % sorted(RETENTIONS))
        if not _not_bool_int(self.max_entries) \
                or not 1 <= self.max_entries <= 10000:
            issues.append("memory_policy.max_entries must be 1-10000")
        if not _not_bool_int(self.max_bytes_per_entry) \
                or not 1 <= self.max_bytes_per_entry <= 1048576:
            issues.append("memory_policy.max_bytes_per_entry must be "
                          "1-1048576")
        # Isolation invariant: cross-agent reads can never be requested.
        if self.allow_cross_agent_read:
            issues.append("memory_policy.allow_cross_agent_read must stay "
                          "false: agents are isolated from each other's "
                          "memory")
        return issues

    def to_dict(self) -> dict[str, Any]:
        return {"retention": self.retention,
                "max_entries": self.max_entries,
                "max_bytes_per_entry": self.max_bytes_per_entry,
                "allow_cross_agent_read": self.allow_cross_agent_read}

    @classmethod
    def from_dict(cls, payload: Any) -> "MemoryPolicy":
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValueError("memory_policy must be an object")
        return cls(
            retention=str(payload.get("retention", "session")).strip(
            ).lower(),
            max_entries=_strict_int(payload.get("max_entries", 200),
                                    "memory_policy.max_entries"),
            max_bytes_per_entry=_strict_int(
                payload.get("max_bytes_per_entry", 20480),
                "memory_policy.max_bytes_per_entry"),
            allow_cross_agent_read=bool(
                payload.get("allow_cross_agent_read", False)))


@dataclass(frozen=True)
class VerificationRequirements:
    require_tests: bool = False
    require_review: bool = True
    require_security_scan: bool = True
    min_benchmark_score: float = 1.0

    def validate(self) -> list[str]:
        issues: list[str] = []
        score = self.min_benchmark_score
        if isinstance(score, bool) \
                or not isinstance(score, (int, float)) \
                or not 0.0 <= float(score) <= 1.0:
            issues.append("verification_requirements.min_benchmark_score "
                          "must be 0.0-1.0")
        return issues

    def to_dict(self) -> dict[str, Any]:
        return {"require_tests": self.require_tests,
                "require_review": self.require_review,
                "require_security_scan": self.require_security_scan,
                "min_benchmark_score": float(self.min_benchmark_score)}

    @classmethod
    def from_dict(cls, payload: Any) -> "VerificationRequirements":
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValueError("verification_requirements must be an object")
        return cls(
            require_tests=bool(payload.get("require_tests", False)),
            require_review=bool(payload.get("require_review", True)),
            require_security_scan=bool(
                payload.get("require_security_scan", True)),
            min_benchmark_score=_strict_float(
                payload.get("min_benchmark_score", 1.0),
                "verification_requirements.min_benchmark_score"))


@dataclass(frozen=True)
class ResourceLimits:
    max_runs_per_hour: int = 60
    max_concurrent: int = 2
    max_tool_calls_per_run: int = 50
    max_wall_seconds: float = 600.0

    def validate(self) -> list[str]:
        issues: list[str] = []
        if not _not_bool_int(self.max_runs_per_hour) \
                or not 1 <= self.max_runs_per_hour <= 1000:
            issues.append("resource_limits.max_runs_per_hour must be "
                          "1-1000")
        if not _not_bool_int(self.max_concurrent) \
                or not 1 <= self.max_concurrent <= 20:
            issues.append("resource_limits.max_concurrent must be 1-20")
        if not _not_bool_int(self.max_tool_calls_per_run) \
                or not 0 <= self.max_tool_calls_per_run <= 500:
            issues.append("resource_limits.max_tool_calls_per_run must be "
                          "0-500")
        if isinstance(self.max_wall_seconds, bool) \
                or not isinstance(self.max_wall_seconds, (int, float)) \
                or not 1 <= float(self.max_wall_seconds) <= 3600:
            issues.append("resource_limits.max_wall_seconds must be "
                          "1-3600")
        return issues

    def to_dict(self) -> dict[str, Any]:
        return {"max_runs_per_hour": self.max_runs_per_hour,
                "max_concurrent": self.max_concurrent,
                "max_tool_calls_per_run": self.max_tool_calls_per_run,
                "max_wall_seconds": float(self.max_wall_seconds)}

    @classmethod
    def from_dict(cls, payload: Any) -> "ResourceLimits":
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValueError("resource_limits must be an object")
        return cls(
            max_runs_per_hour=_strict_int(
                payload.get("max_runs_per_hour", 60),
                "resource_limits.max_runs_per_hour"),
            max_concurrent=_strict_int(
                payload.get("max_concurrent", 2),
                "resource_limits.max_concurrent"),
            max_tool_calls_per_run=_strict_int(
                payload.get("max_tool_calls_per_run", 50),
                "resource_limits.max_tool_calls_per_run"),
            max_wall_seconds=_strict_float(
                payload.get("max_wall_seconds", 600.0),
                "resource_limits.max_wall_seconds"))


@dataclass(frozen=True)
class AgentSpec:
    """The nine-dimension specification for one specialized agent."""

    name: str
    purpose: str
    capabilities: tuple[str, ...] = ("coding",)
    tools: tuple[str, ...] = ("read_file", "memory_read")
    permissions: tuple[PermissionRequestSpec, ...] = ()
    model_requirements: ModelRequirements = field(
        default_factory=ModelRequirements)
    memory_policy: MemoryPolicy = field(default_factory=MemoryPolicy)
    verification_requirements: VerificationRequirements = field(
        default_factory=VerificationRequirements)
    resource_limits: ResourceLimits = field(
        default_factory=ResourceLimits)
    role: str = ""
    template: str = ""

    def validate(self) -> list[str]:
        issues: list[str] = []
        issues.extend(_issues_for_name(self.name))
        if not isinstance(self.purpose, str) or not self.purpose.strip() \
                or len(self.purpose) > MAX_PURPOSE:
            issues.append("purpose must be 1-%d characters" % MAX_PURPOSE)
        caps = tuple(self.capabilities or ())
        if not caps or len(caps) > MAX_CAPABILITIES:
            issues.append("capabilities must hold 1-%d entries"
                          % MAX_CAPABILITIES)
        elif len(set(caps)) != len(caps):
            issues.append("capabilities must not repeat")
        elif any(not is_capability(cap) for cap in caps):
            issues.append("capabilities must come from the canonical "
                          "vocabulary: %s"
                          % (", ".join(ALL_CAPABILITIES),))
        tools = tuple(self.tools or ())
        if len(tools) > MAX_TOOLS:
            issues.append("tools must hold at most %d entries" % MAX_TOOLS)
        elif len(set(tools)) != len(tools):
            issues.append("tools must not repeat")
        elif any(tool not in KNOWN_TOOLS for tool in tools):
            issues.append("tools must come from the known tool set: %s"
                          % (", ".join(sorted(KNOWN_TOOLS)),))
        if len(self.permissions) > MAX_PERMISSIONS:
            issues.append("permissions must hold at most %d entries"
                          % MAX_PERMISSIONS)
        for index, permission in enumerate(self.permissions):
            issues.extend("permissions[%d]: %s" % (index, issue)
                          for issue in permission.validate())
        issues.extend(self.model_requirements.validate())
        issues.extend(self.memory_policy.validate())
        issues.extend(self.verification_requirements.validate())
        issues.extend(self.resource_limits.validate())
        if self.role and (
                not isinstance(self.role, str)
                or not ROLE_RE.match(self.role.strip().lower())):
            issues.append("role must match [a-z][a-z0-9_-]{2,32}")
        if self.template and (
                not isinstance(self.template, str)
                or self.template.strip().lower() not in TEMPLATES):
            issues.append("template %r is unknown; expected one of %s"
                          % (self.template, sorted(TEMPLATES)))
        if self.verification_requirements.require_tests \
                and "run_tests" not in tools:
            issues.append("verification_requirements.require_tests needs "
                          "the run_tests tool: the mediated runtime runs "
                          "the project pytest suite and nothing else")
        # Model requirements must be satisfiable by the agent's own
        # capabilities: an agent cannot require what it does not declare.
        for cap in self.model_requirements.capabilities:
            if cap not in caps:
                issues.append(
                    "model_requirements capability %r is not among the "
                    "agent's declared capabilities" % (cap,))
        return issues

    def check(self) -> "AgentSpec":
        issues = self.validate()
        if issues:
            raise ValueError("; ".join(issues))
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec_version": SPEC_VERSION,
            "name": self.name,
            "purpose": self.purpose,
            "capabilities": list(self.capabilities),
            "tools": list(self.tools),
            "permissions": [permission.to_dict()
                            for permission in self.permissions],
            "model_requirements": self.model_requirements.to_dict(),
            "memory_policy": self.memory_policy.to_dict(),
            "verification_requirements":
                self.verification_requirements.to_dict(),
            "resource_limits": self.resource_limits.to_dict(),
            "role": self.role,
            "template": self.template,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "AgentSpec":
        if not isinstance(payload, dict):
            raise ValueError("an agent spec must be an object")
        version = payload.get("spec_version", SPEC_VERSION)
        if version is None:
            version = SPEC_VERSION
        if not isinstance(version, str) or not SPEC_VERSION_RE.match(
                version.strip().lower()):
            raise ValueError(
                "spec_version %r is unknown; expected something like %r"
                % (payload.get("spec_version"), SPEC_VERSION))
        raw_name = payload.get("name", "")
        if raw_name is None:
            raw_name = ""
        if not isinstance(raw_name, str):
            raise ValueError("name must be a string")
        raw_purpose = payload.get("purpose", "")
        if raw_purpose is None:
            raw_purpose = ""
        if not isinstance(raw_purpose, str):
            raise ValueError("purpose must be a string")
        permissions = payload.get("permissions", [])
        if not isinstance(permissions, list):
            raise ValueError("permissions must be a list")
        raw_role = payload.get("role", "")
        if raw_role is None:
            raw_role = ""
        if not isinstance(raw_role, str):
            raise ValueError("role must be a string")
        raw_template = payload.get("template", "")
        if raw_template is None:
            raw_template = ""
        if not isinstance(raw_template, str):
            raise ValueError("template must be a string")
        return cls(
            name=raw_name.strip().lower(),
            purpose=raw_purpose.strip(),
            capabilities=_strict_str_list(payload.get("capabilities", []),
                                          "capabilities"),
            tools=_strict_str_list(payload.get("tools", []), "tools"),
            permissions=tuple(PermissionRequestSpec.from_dict(entry)
                              for entry in permissions),
            model_requirements=ModelRequirements.from_dict(
                payload.get("model_requirements")),
            memory_policy=MemoryPolicy.from_dict(
                payload.get("memory_policy")),
            verification_requirements=VerificationRequirements.from_dict(
                payload.get("verification_requirements")),
            resource_limits=ResourceLimits.from_dict(
                payload.get("resource_limits")),
            role=raw_role.strip().lower(),
            template=raw_template.strip().lower(),
        )


# -- first-party templates -------------------------------------------------

def _perm(resource: str, operation: str, scope: str, risk: str,
          reason: str) -> dict[str, str]:
    return {"resource": resource, "operation": operation, "scope": scope,
            "risk": risk, "reason": reason}


TEMPLATES: dict[str, dict[str, Any]] = {
    "coding": {
        "role": "coding",
        "purpose": ("Implement, modify, and repair repository code through "
                    "validated change sets, tests, and review."),
        "capabilities": ["coding", "debugging", "testing", "review"],
        "tools": ["read_file", "write_file", "search", "run_tests",
                  "git_status", "memory_read", "memory_write"],
        "permissions": [
            _perm("filesystem", "read", "**",
                  "LOW", "Read project sources to build change context."),
            _perm("filesystem", "write", "src/**",
                  "MEDIUM", "Write implementation files via change sets."),
            _perm("terminal", "execute", "pytest",
                  "MEDIUM", "Run the repository test suite."),
            _perm("git", "status", "",
                  "LOW", "Inspect the working tree status (read-only)."),
            _perm("memory", "read", "",
                  "LOW", "Recall the agent's own prior run notes."),
            _perm("memory", "write", "",
                  "LOW", "Record the agent's own run notes."),
        ],
        "model_requirements": {"capabilities": ["coding"],
                               "min_context_window": 8192},
        "memory_policy": {"retention": "persistent", "max_entries": 300},
        "verification_requirements": {"require_tests": True,
                                      "require_review": True,
                                      "require_security_scan": True,
                                      "min_benchmark_score": 0.8},
        "resource_limits": {"max_runs_per_hour": 60, "max_concurrent": 2,
                            "max_tool_calls_per_run": 50,
                            "max_wall_seconds": 600.0},
    },
    "research": {
        "role": "research",
        "purpose": ("Investigate repositories, documents, and the web, then "
                    "report findings with sources. Never modifies code."),
        "capabilities": ["research", "reasoning", "documentation"],
        "tools": ["read_file", "search", "git_status", "memory_read",
                  "memory_write"],
        "permissions": [
            _perm("filesystem", "read", "**",
                  "LOW", "Read-only repository investigation."),
            _perm("browser", "read", "public-docs",
                  "LOW", "Read public documentation and sources."),
            _perm("network", "request", "docs/**",
                  "LOW", "Fetch cited reference pages."),
            _perm("git", "status", "",
                  "LOW", "Inspect the working tree status (read-only)."),
            _perm("memory", "read", "",
                  "LOW", "Recall the agent's own prior findings."),
            _perm("memory", "write", "",
                  "LOW", "Record the agent's own findings."),
        ],
        "model_requirements": {"capabilities": ["research"],
                               "min_context_window": 16384},
        "memory_policy": {"retention": "persistent", "max_entries": 500},
        "verification_requirements": {"require_tests": False,
                                      "require_review": True,
                                      "require_security_scan": True,
                                      "min_benchmark_score": 0.8},
        "resource_limits": {"max_runs_per_hour": 60, "max_concurrent": 3,
                            "max_tool_calls_per_run": 80,
                            "max_wall_seconds": 900.0},
    },
    "security": {
        "role": "security",
        "purpose": ("Scan code and dependencies for secrets, dangerous "
                    "patterns, and vulnerabilities, then report severity-"
                    "typed findings. Never modifies code."),
        "capabilities": ["security", "review", "testing"],
        "tools": ["read_file", "search", "git_status", "memory_read",
                  "memory_write"],
        "permissions": [
            _perm("filesystem", "read", "**",
                  "LOW", "Read-only security inspection."),
            _perm("terminal", "execute", "security-scan",
                  "MEDIUM", "Run read-only scanners and test collectors."),
            _perm("git", "status", "",
                  "LOW", "Inspect the working tree status (read-only)."),
            _perm("memory", "read", "",
                  "LOW", "Recall the agent's own prior findings."),
            _perm("memory", "write", "",
                  "LOW", "Record the agent's own findings."),
        ],
        "model_requirements": {"capabilities": ["security"],
                               "min_context_window": 8192},
        "memory_policy": {"retention": "persistent", "max_entries": 300},
        "verification_requirements": {"require_tests": False,
                                      "require_review": True,
                                      "require_security_scan": True,
                                      "min_benchmark_score": 1.0},
        "resource_limits": {"max_runs_per_hour": 30, "max_concurrent": 1,
                            "max_tool_calls_per_run": 40,
                            "max_wall_seconds": 600.0},
    },
    "game-dev": {
        "role": "game-development",
        "purpose": ("Design and implement game systems, scenes, and asset "
                    "pipelines through validated change sets and tests."),
        "capabilities": ["coding", "planning", "testing",
                         "documentation"],
        "tools": ["read_file", "write_file", "search", "run_tests",
                  "git_status", "memory_read", "memory_write"],
        "permissions": [
            _perm("filesystem", "read", "**",
                  "LOW", "Read game project sources and assets."),
            _perm("filesystem", "write", "game/**",
                  "MEDIUM", "Write game code and scene files."),
            _perm("terminal", "execute", "pytest",
                  "MEDIUM", "Run game logic tests."),
            _perm("git", "status", "",
                  "LOW", "Inspect the working tree status (read-only)."),
            _perm("memory", "read", "",
                  "LOW", "Recall the agent's own prior run notes."),
            _perm("memory", "write", "",
                  "LOW", "Record the agent's own run notes."),
        ],
        "model_requirements": {"capabilities": ["coding"],
                               "min_context_window": 8192},
        "memory_policy": {"retention": "persistent", "max_entries": 300},
        "verification_requirements": {"require_tests": True,
                                      "require_review": True,
                                      "require_security_scan": True,
                                      "min_benchmark_score": 0.8},
        "resource_limits": {"max_runs_per_hour": 60, "max_concurrent": 2,
                            "max_tool_calls_per_run": 60,
                            "max_wall_seconds": 900.0},
    },
    "os-dev": {
        "role": "os-development",
        "purpose": ("Implement operating-system components — kernels, "
                    "drivers, and tooling — with strict verification and "
                    "bounded, reviewed change sets."),
        "capabilities": ["coding", "debugging", "testing", "security"],
        "tools": ["read_file", "write_file", "search", "run_tests",
                  "git_status", "memory_read", "memory_write"],
        "permissions": [
            _perm("filesystem", "read", "**",
                  "LOW", "Read OS sources and build files."),
            _perm("filesystem", "write", "kernel/**",
                  "HIGH", "Write OS component sources via review."),
            _perm("terminal", "execute", "pytest",
                  "HIGH", "Run the OS component pytest suite."),
            _perm("git", "status", "",
                  "LOW", "Inspect the working tree status (read-only)."),
            _perm("memory", "read", "",
                  "LOW", "Recall the agent's own prior run notes."),
            _perm("memory", "write", "",
                  "LOW", "Record the agent's own run notes."),
        ],
        "model_requirements": {"capabilities": ["coding"],
                               "min_context_window": 16384},
        "memory_policy": {"retention": "persistent", "max_entries": 300},
        "verification_requirements": {"require_tests": True,
                                      "require_review": True,
                                      "require_security_scan": True,
                                      "min_benchmark_score": 1.0},
        "resource_limits": {"max_runs_per_hour": 20, "max_concurrent": 1,
                            "max_tool_calls_per_run": 40,
                            "max_wall_seconds": 1200.0},
    },
    "documentation": {
        "role": "documentation",
        "purpose": ("Write and maintain accurate documentation from real "
                    "repository content. Never modifies code."),
        "capabilities": ["documentation", "research"],
        "tools": ["read_file", "write_file", "search", "memory_read",
                  "memory_write"],
        "permissions": [
            _perm("filesystem", "read", "**",
                  "LOW", "Read sources to document real behavior."),
            _perm("filesystem", "write", "docs/**",
                  "LOW", "Write documentation files only."),
            _perm("memory", "read", "",
                  "LOW", "Recall the agent's own prior notes."),
            _perm("memory", "write", "",
                  "LOW", "Record the agent's own notes."),
        ],
        "model_requirements": {"capabilities": ["documentation"],
                               "min_context_window": 8192},
        "memory_policy": {"retention": "persistent", "max_entries": 300},
        "verification_requirements": {"require_tests": False,
                                      "require_review": True,
                                      "require_security_scan": True,
                                      "min_benchmark_score": 0.8},
        "resource_limits": {"max_runs_per_hour": 60, "max_concurrent": 2,
                            "max_tool_calls_per_run": 30,
                            "max_wall_seconds": 600.0},
    },
}


def template_names() -> list[str]:
    """Template ids in deterministic order."""
    return sorted(TEMPLATES)


def build_template(template: str, name: str,
                   overrides: dict[str, Any] | None = None) -> AgentSpec:
    """Build a validated spec from a first-party template.

    ``overrides`` replaces top-level template fields (nested objects are
    merged key-wise); the result always re-validates, so an override can
    never smuggle an invalid spec past the template.
    """
    key = (template or "").strip().lower()
    if key not in TEMPLATES:
        raise ValueError("Unknown template %r; expected one of %s"
                         % (template, sorted(TEMPLATES)))
    base: dict[str, Any] = {
        field: (list(value) if isinstance(value, list)
                else dict(value) if isinstance(value, dict) else value)
        for field, value in TEMPLATES[key].items()
    }
    for field, value in (overrides or {}).items():
        if field in ("model_requirements", "memory_policy",
                     "verification_requirements", "resource_limits") \
                and isinstance(value, dict) \
                and isinstance(base.get(field), dict):
            merged = dict(base[field])
            merged.update(value)
            base[field] = merged
        else:
            base[field] = value
    base["name"] = name
    base["template"] = key
    return AgentSpec.from_dict(base).check()
