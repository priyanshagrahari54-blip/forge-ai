"""Agent specifications (A83): the contract an agent is built from.

An :class:`AgentSpec` is the *only* source of an agent's power. It
declares what the agent is for, which capabilities it routes on, which
tools it may call, which operations an operator has granted it, which
models may serve it, how much memory it keeps, what verification must
pass, and how many resources it may consume.

Three rules are enforced here rather than trusted at run time:

* **The vocabulary is closed.** Capabilities come from the canonical
  Model Fabric vocabulary, tools from the canonical runtime tool
  catalog, and operations from the canonical permission vocabulary.
  Anything else is a validation error, never a silent pass.
* **The floor cannot be lowered.** Protected paths (``.git``,
  ``.forge``, ``.env``, credential material) are always denied, and the
  permanently blocked operations can never be granted.
* **A tool implies its operation.** Declaring ``write_file`` without the
  ``write_file`` operation is inconsistent and fails validation, so a
  package can never claim a capability it is not granted.

Creating or editing a spec grants nothing by itself: power exists only
once an operator records grants against the spec (see
:mod:`forge.agents.engine.grants`) and the PolicyGate agrees at run
time.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

from forge.models.capabilities import ALL_CAPABILITIES, is_capability

#: Agent names: lowercase, start with a letter, filesystem-safe.
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{2,48}$")
ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{2,32}$")
TAG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")

MAX_CAPABILITIES = 12
MAX_TOOLS = 8
MAX_TAGS = 8
MAX_PURPOSE = 400
MIN_PURPOSE = 10
MAX_PATH_RULES = 24
MAX_DESCRIPTION = 500


@dataclass(frozen=True)
class ToolInfo:
    """Canonical metadata for one runtime tool an agent may be granted."""

    name: str
    operation: str
    risk: str
    writes: bool
    #: The only argument names this tool accepts. Model-supplied arguments
    #: are filtered against this list, so a response cannot smuggle extra
    #: keyword arguments into a handler.
    arguments: tuple = ()


#: The canonical tool catalog. These are exactly the tools registered by
#: :func:`forge.runtime.defaults.create_default_runtime`; the engine never
#: invents a tool, and a spec naming one of these is still refused at run
#: time when the runtime in use does not register it.
TOOL_CATALOG: dict = {
    "read_file": ToolInfo("read_file", "read_file", "NONE", False,
                          ("path",)),
    "search": ToolInfo("search", "search_files", "NONE", False,
                       ("query",)),
    "git_status": ToolInfo("git_status", "git_status", "NONE", False, ()),
    "run_tests": ToolInfo("run_tests", "run_tests", "LOW", False,
                          ("command",)),
    "write_file": ToolInfo("write_file", "write_file", "MEDIUM", True,
                           ("path", "content")),
    "delete_file": ToolInfo("delete_file", "delete_file", "HIGH", True,
                            ("path",)),
    "terminal": ToolInfo("terminal", "run_command", "HIGH", True,
                         ("command", "timeout")),
}

#: Operations an operator may grant to an agent.
GRANTABLE_OPERATIONS: tuple = (
    "read_file", "search_files", "git_status", "git_diff", "run_tests",
    "write_file", "delete_file", "run_command", "git_commit", "git_push",
)

#: Operations that are blocked for everyone, always. A spec asking for
#: one of these is invalid, and no grant can ever produce them.
FORBIDDEN_OPERATIONS: tuple = ("delete_repository", "expose_secrets")

#: Session-mode ceilings a spec may declare (most to least restrictive).
MODE_CEILINGS: tuple = ("locked", "safe", "assisted", "autonomous")

MEMORY_SCOPES: tuple = ("none", "agent", "project")

#: Path components that are denied for every agent, whatever its spec
#: says. Declaring them as allowed is a validation error, not an override.
PROTECTED_PATH_PARTS: tuple = (".git", ".forge")

#: Highest allowed values, so a spec cannot ask for "unbounded".
MAX_RUNS_PER_HOUR = 500
MAX_CONCURRENT = 8
MAX_WALL_SECONDS = 3600.0
MAX_MODEL_CALLS = 32
MAX_FILE_WRITES = 200
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_MEMORY_ENTRIES = 500
MAX_MEMORY_ENTRY_BYTES = 256 * 1024


def _clean_tuple(values: Any, limit: int, label: str) -> tuple:
    """Normalize an iterable of strings into a bounded, deduped tuple."""
    if values is None:
        return ()
    if isinstance(values, str):
        raise ValueError("%s must be a list of strings, not a string" % label)
    try:
        items = list(values)
    except TypeError:
        raise ValueError("%s must be a list of strings" % label) from None
    cleaned: list = []
    for item in items:
        if not isinstance(item, str):
            raise ValueError("%s must contain only strings" % label)
        value = item.strip()
        if value and value not in cleaned:
            cleaned.append(value)
    if len(cleaned) > limit:
        raise ValueError("%s accepts at most %d entries" % (label, limit))
    return tuple(cleaned)


def _positive_int(value: Any, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be an integer" % label)
    if value < 1 or value > maximum:
        raise ValueError("%s must be between 1 and %d" % (label, maximum))
    return value


def _non_negative_number(value: Any, label: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s must be a number" % label)
    number = float(value)
    if number < 0 or number > maximum:
        raise ValueError("%s must be between 0 and %s" % (label, maximum))
    return number


@dataclass(frozen=True)
class ToolGrant:
    """One tool an agent may call, with a per-run call bound."""

    name: str
    max_calls: int = 8

    @property
    def info(self) -> ToolInfo:
        return TOOL_CATALOG[self.name]

    def to_dict(self) -> dict:
        return {"name": self.name, "max_calls": self.max_calls}

    @classmethod
    def from_dict(cls, payload: Any) -> "ToolGrant":
        if isinstance(payload, str):
            return cls(name=payload)
        if not isinstance(payload, dict):
            raise ValueError("tools entries must be objects or strings")
        return cls(
            name=str(payload.get("name", "")),
            max_calls=_positive_int(payload.get("max_calls", 8),
                                    "tools.max_calls", 64),
        )


@dataclass(frozen=True)
class PermissionSpec:
    """Operator-declared permission posture for one agent.

    ``operations`` is a *ceiling*, not an active grant: the runtime
    additionally requires a recorded grant and a PolicyGate ``ALLOW``.
    """

    operations: tuple = ()
    mode_ceiling: str = "assisted"
    allowed_paths: tuple = ()
    denied_paths: tuple = ()

    def to_dict(self) -> dict:
        return {
            "operations": list(self.operations),
            "mode_ceiling": self.mode_ceiling,
            "allowed_paths": list(self.allowed_paths),
            "denied_paths": list(self.denied_paths),
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "PermissionSpec":
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValueError("permissions must be an object")
        return cls(
            operations=_clean_tuple(payload.get("operations"),
                                    len(GRANTABLE_OPERATIONS),
                                    "permissions.operations"),
            mode_ceiling=str(payload.get("mode_ceiling", "assisted")),
            allowed_paths=_clean_tuple(payload.get("allowed_paths"),
                                       MAX_PATH_RULES,
                                       "permissions.allowed_paths"),
            denied_paths=_clean_tuple(payload.get("denied_paths"),
                                      MAX_PATH_RULES,
                                      "permissions.denied_paths"),
        )


@dataclass(frozen=True)
class ModelRequirements:
    """Which models may serve this agent, and on what capability."""

    capabilities: tuple = ("coding",)
    min_context_window: int = 0
    max_latency_ms: float = 0.0
    max_cost_per_token: float = 0.0
    max_output_tokens: int = 0
    prefer_local: bool = True
    prefer_free: bool = True
    #: ``False`` (default) means a run served by the offline placeholder
    #: fails honestly instead of pretending to be model output.
    allow_fallback: bool = False

    def to_dict(self) -> dict:
        return {
            "capabilities": list(self.capabilities),
            "min_context_window": self.min_context_window,
            "max_latency_ms": self.max_latency_ms,
            "max_cost_per_token": self.max_cost_per_token,
            "max_output_tokens": self.max_output_tokens,
            "prefer_local": self.prefer_local,
            "prefer_free": self.prefer_free,
            "allow_fallback": self.allow_fallback,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "ModelRequirements":
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValueError("model must be an object")
        return cls(
            capabilities=_clean_tuple(payload.get("capabilities"),
                                      MAX_CAPABILITIES,
                                      "model.capabilities") or ("coding",),
            min_context_window=int(_non_negative_number(
                payload.get("min_context_window", 0),
                "model.min_context_window", 10_000_000)),
            max_latency_ms=_non_negative_number(
                payload.get("max_latency_ms", 0), "model.max_latency_ms",
                600_000.0),
            max_cost_per_token=_non_negative_number(
                payload.get("max_cost_per_token", 0),
                "model.max_cost_per_token", 100.0),
            max_output_tokens=int(_non_negative_number(
                payload.get("max_output_tokens", 0),
                "model.max_output_tokens", 1_000_000)),
            prefer_local=bool(payload.get("prefer_local", True)),
            prefer_free=bool(payload.get("prefer_free", True)),
            allow_fallback=bool(payload.get("allow_fallback", False)),
        )


@dataclass(frozen=True)
class MemoryPolicy:
    """How much durable memory an agent keeps, and where.

    ``agent`` scope is a private namespace only this agent can read.
    ``project`` scope adds a shared project namespace for agents that
    declare it. ``none`` disables memory entirely. There is no scope
    that exposes one agent's private namespace to another.
    """

    scope: str = "agent"
    max_entries: int = 64
    max_entry_bytes: int = 4096
    ttl_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "max_entries": self.max_entries,
            "max_entry_bytes": self.max_entry_bytes,
            "ttl_seconds": self.ttl_seconds,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "MemoryPolicy":
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValueError("memory must be an object")
        return cls(
            scope=str(payload.get("scope", "agent")),
            max_entries=_positive_int(payload.get("max_entries", 64),
                                      "memory.max_entries",
                                      MAX_MEMORY_ENTRIES),
            max_entry_bytes=_positive_int(
                payload.get("max_entry_bytes", 4096),
                "memory.max_entry_bytes", MAX_MEMORY_ENTRY_BYTES),
            ttl_seconds=_non_negative_number(payload.get("ttl_seconds", 0),
                                             "memory.ttl_seconds",
                                             31_536_000.0),
        )


@dataclass(frozen=True)
class VerificationSpec:
    """Verification that must pass before work is accepted.

    ``require_*`` gates run per agent run; ``min_benchmark_pass_rate``
    and ``required_scenarios`` gate the ``tested`` lifecycle state.
    """

    require_security_scan: bool = True
    require_tests: bool = False
    require_review: bool = False
    max_security_findings: int = 0
    min_benchmark_pass_rate: float = 1.0
    required_scenarios: tuple = ()

    def to_dict(self) -> dict:
        return {
            "require_security_scan": self.require_security_scan,
            "require_tests": self.require_tests,
            "require_review": self.require_review,
            "max_security_findings": self.max_security_findings,
            "min_benchmark_pass_rate": self.min_benchmark_pass_rate,
            "required_scenarios": list(self.required_scenarios),
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "VerificationSpec":
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValueError("verification must be an object")
        return cls(
            require_security_scan=bool(
                payload.get("require_security_scan", True)),
            require_tests=bool(payload.get("require_tests", False)),
            require_review=bool(payload.get("require_review", False)),
            max_security_findings=int(_non_negative_number(
                payload.get("max_security_findings", 0),
                "verification.max_security_findings", 100)),
            min_benchmark_pass_rate=_non_negative_number(
                payload.get("min_benchmark_pass_rate", 1.0),
                "verification.min_benchmark_pass_rate", 1.0),
            required_scenarios=_clean_tuple(
                payload.get("required_scenarios"), 16,
                "verification.required_scenarios"),
        )


@dataclass(frozen=True)
class ResourceLimits:
    """Hard bounds on what one agent may consume."""

    max_runs_per_hour: int = 20
    max_concurrent: int = 1
    max_wall_seconds: float = 120.0
    max_model_calls: int = 4
    max_file_writes: int = 8
    max_output_bytes: int = 262_144

    def to_dict(self) -> dict:
        return {
            "max_runs_per_hour": self.max_runs_per_hour,
            "max_concurrent": self.max_concurrent,
            "max_wall_seconds": self.max_wall_seconds,
            "max_model_calls": self.max_model_calls,
            "max_file_writes": self.max_file_writes,
            "max_output_bytes": self.max_output_bytes,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "ResourceLimits":
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValueError("limits must be an object")
        return cls(
            max_runs_per_hour=_positive_int(
                payload.get("max_runs_per_hour", 20),
                "limits.max_runs_per_hour", MAX_RUNS_PER_HOUR),
            max_concurrent=_positive_int(payload.get("max_concurrent", 1),
                                         "limits.max_concurrent",
                                         MAX_CONCURRENT),
            max_wall_seconds=_non_negative_number(
                payload.get("max_wall_seconds", 120.0),
                "limits.max_wall_seconds", MAX_WALL_SECONDS) or 1.0,
            max_model_calls=_positive_int(payload.get("max_model_calls", 4),
                                          "limits.max_model_calls",
                                          MAX_MODEL_CALLS),
            max_file_writes=_positive_int(payload.get("max_file_writes", 8),
                                          "limits.max_file_writes",
                                          MAX_FILE_WRITES),
            max_output_bytes=_positive_int(
                payload.get("max_output_bytes", 262_144),
                "limits.max_output_bytes", MAX_OUTPUT_BYTES),
        )


@dataclass(frozen=True)
class AgentSpec:
    """A complete, validated agent specification."""

    name: str
    purpose: str
    capabilities: tuple
    tools: tuple = ()
    permissions: PermissionSpec = field(default_factory=PermissionSpec)
    model: ModelRequirements = field(default_factory=ModelRequirements)
    memory: MemoryPolicy = field(default_factory=MemoryPolicy)
    verification: VerificationSpec = field(default_factory=VerificationSpec)
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    role: str = ""
    template: str = ""
    tags: tuple = ()

    # -- derived views ---------------------------------------------------

    def tool_names(self) -> tuple:
        return tuple(tool.name for tool in self.tools)

    def required_operations(self) -> tuple:
        """Operations implied by the declared tools (must be granted).

        Tools outside the catalog are skipped here — they are reported by
        :meth:`findings`, and validation must be able to describe a broken
        spec rather than raise on it.
        """
        return tuple(dict.fromkeys(
            TOOL_CATALOG[tool.name].operation for tool in self.tools
            if tool.name in TOOL_CATALOG))

    def writes_files(self) -> bool:
        return any(TOOL_CATALOG[tool.name].writes for tool in self.tools)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "role": self.role,
            "template": self.template,
            "tags": list(self.tags),
            "capabilities": list(self.capabilities),
            "tools": [tool.to_dict() for tool in self.tools],
            "permissions": self.permissions.to_dict(),
            "model": self.model.to_dict(),
            "memory": self.memory.to_dict(),
            "verification": self.verification.to_dict(),
            "limits": self.limits.to_dict(),
        }

    def canonical(self) -> str:
        """Stable JSON used for fingerprinting and version diffs."""
        return json.dumps(self.to_dict(), sort_keys=True,
                          separators=(",", ":"))

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()

    def renamed(self, name: str) -> "AgentSpec":
        return replace(self, name=name)

    # -- construction ----------------------------------------------------

    @classmethod
    def from_dict(cls, payload: Any) -> "AgentSpec":
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError as exc:
                raise ValueError("Malformed JSON specification: %s" % exc)
        if not isinstance(payload, dict):
            raise ValueError("A specification must be a JSON object")
        capabilities = _clean_tuple(payload.get("capabilities"),
                                    MAX_CAPABILITIES, "capabilities")
        tools = payload.get("tools") or ()
        if isinstance(tools, (str, dict)):
            tools = (tools,)
        grants = tuple(ToolGrant.from_dict(item) for item in tools)
        return cls(
            name=str(payload.get("name", "")),
            purpose=str(payload.get("purpose", "")),
            capabilities=capabilities,
            tools=grants,
            permissions=PermissionSpec.from_dict(payload.get("permissions")),
            model=ModelRequirements.from_dict(payload.get("model")),
            memory=MemoryPolicy.from_dict(payload.get("memory")),
            verification=VerificationSpec.from_dict(
                payload.get("verification")),
            limits=ResourceLimits.from_dict(payload.get("limits")),
            role=str(payload.get("role", "") or ""),
            template=str(payload.get("template", "") or ""),
            tags=_clean_tuple(payload.get("tags"), MAX_TAGS, "tags"),
        )

    # -- validation ------------------------------------------------------

    def findings(self) -> tuple:
        """Return every validation problem, in a stable order."""
        problems: list = []
        name = (self.name or "").strip()
        if not NAME_PATTERN.match(name):
            problems.append(
                "name must match [a-z][a-z0-9_-]{2,48}")
        purpose = (self.purpose or "").strip()
        if len(purpose) < MIN_PURPOSE:
            problems.append(
                "purpose must be at least %d characters" % MIN_PURPOSE)
        if len(purpose) > MAX_PURPOSE:
            problems.append(
                "purpose must be at most %d characters" % MAX_PURPOSE)
        if self.role and not ROLE_PATTERN.match(self.role.strip()):
            problems.append("role must match [a-z][a-z0-9_-]{2,32}")
        for tag in self.tags:
            if not TAG_PATTERN.match(tag):
                problems.append("invalid tag: %r" % tag)
        if not self.capabilities:
            problems.append("capabilities must not be empty")
        for capability in self.capabilities:
            if not is_capability(capability):
                problems.append(
                    "unknown capability %r (vocabulary: %s)"
                    % (capability, ", ".join(ALL_CAPABILITIES)))

        seen: set = set()
        for tool in self.tools:
            if tool.name in seen:
                problems.append("duplicate tool: %r" % tool.name)
            seen.add(tool.name)
            if tool.name not in TOOL_CATALOG:
                problems.append(
                    "unknown tool %r (catalog: %s)"
                    % (tool.name, ", ".join(sorted(TOOL_CATALOG))))
        required = set()
        for tool in self.tools:
            info = TOOL_CATALOG.get(tool.name)
            if info is not None:
                required.add(info.operation)
        granted = set(self.permissions.operations)
        for operation in sorted(granted):
            if operation in FORBIDDEN_OPERATIONS:
                problems.append(
                    "operation %r is permanently blocked and cannot be "
                    "granted" % operation)
            elif operation not in GRANTABLE_OPERATIONS:
                problems.append("unknown permission operation %r" % operation)
        missing = sorted(required - granted)
        if missing:
            problems.append(
                "tools require operations that are not granted: %s"
                % ", ".join(missing))
        if self.permissions.mode_ceiling not in MODE_CEILINGS:
            problems.append(
                "mode_ceiling must be one of %s"
                % ", ".join(MODE_CEILINGS))
        for rule in self.permissions.denied_paths:
            problems.extend(_path_findings(rule, "permissions.denied_paths"))
        for rule in self.permissions.allowed_paths:
            problems.extend(_path_findings(rule, "permissions.allowed_paths"))
            for part in PROTECTED_PATH_PARTS:
                if rule == part or rule.startswith(part + "/"):
                    problems.append(
                        "permissions.allowed_paths cannot un-protect %r"
                        % rule)

        for capability in self.model.capabilities:
            if not is_capability(capability):
                problems.append(
                    "model.capabilities contains unknown capability %r"
                    % capability)
        if not self.model.capabilities:
            problems.append("model.capabilities must not be empty")
        if self.memory.scope not in MEMORY_SCOPES:
            problems.append(
                "memory.scope must be one of %s" % ", ".join(MEMORY_SCOPES))
        rate = self.verification.min_benchmark_pass_rate
        if rate < 0.0 or rate > 1.0:
            problems.append(
                "verification.min_benchmark_pass_rate must be 0..1")
        if self.verification.require_tests and "run_tests" not in seen:
            problems.append(
                "verification.require_tests needs the run_tests tool")
        return tuple(problems)

    def validate(self) -> "AgentSpec":
        """Return ``self`` or raise with every problem listed at once."""
        from forge.agents.engine.errors import AgentSpecError

        problems = self.findings()
        if problems:
            raise AgentSpecError(
                "Invalid agent specification for %r: %s"
                % (self.name, "; ".join(problems)), problems)
        return self


def _path_findings(rule: str, label: str) -> list:
    """Validate one relative POSIX path rule."""
    problems: list = []
    if not rule or rule.strip() != rule:
        problems.append("%s entries must be non-empty and trimmed" % label)
        return problems
    if rule.startswith("/") or "\\" in rule or ".." in rule.split("/"):
        problems.append(
            "%s entries must be relative POSIX paths without traversal: %r"
            % (label, rule))
    return problems


def validate_spec(payload: Any) -> AgentSpec:
    """Build and validate an :class:`AgentSpec` from a dict/JSON payload."""
    from forge.agents.engine.errors import AgentSpecError

    try:
        spec = AgentSpec.from_dict(payload)
    except ValueError as exc:
        raise AgentSpecError(str(exc), (str(exc),)) from None
    return spec.validate()
