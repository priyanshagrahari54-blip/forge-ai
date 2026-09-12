"""Agent role specifications (A81).

The ten canonical agent roles of the parallel orchestration system, each
described by a complete, machine-checkable specification:

* **capability** — canonical model capabilities the role performs
  (from the A40 capability vocabulary, the single source of truth);
* **permissions** — the (resource, operation, scope) triples the role may
  request. Every dispatch still passes the A33 permission gate; a
  specification can never widen what policy allows, only declare intent;
* **model requirement** — the capabilities a backing model must advertise
  for the role to be considered satisfiable by it (checked against the
  fabric/router capability list, never against configuration existence);
* **task scope** — which task kinds (sequential / parallel / dependent)
  and which domains the role may be assigned;
* **resource limits** — bounded files touched, output bytes, model calls,
  and a hard duration;
* **timeout** — the wall-clock budget for one task attempt;
* **result schema** — the structured result contract the scheduler
  validates after every attempt. A result that violates the schema is a
  failed attempt (retried), never silently accepted.

Specifications are frozen data: execution state never mutates them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from forge.models.capabilities import ALL_CAPABILITIES

#: Task kinds a role may be assigned (see ``forge.orchestration.graph``).
TASK_KINDS = frozenset({"sequential", "parallel", "dependent"})

#: Result field type names understood by :func:`validate_result`.
_FIELD_TYPES = {
    "str": (str,),
    "int": (int,),
    "float": (int, float),
    "bool": (bool,),
    "list": (list, tuple),
    "dict": (dict,),
    "any": (object,),
}


def _union(spec: str) -> tuple:
    """Expand ``"str|list"`` union specs into a type tuple."""
    names = [part.strip() for part in spec.split("|")]
    for name in names:
        if name not in _FIELD_TYPES:
            raise ValueError(f"Unknown result field type: {name!r}")
    return tuple(
        t for name in names for t in _FIELD_TYPES[name]
    )


@dataclass(frozen=True)
class RoleResourceLimits:
    """Bounded resources one task attempt of a role may consume."""

    max_files: int = 25
    max_output_bytes: int = 64 * 1024
    max_model_calls: int = 4
    max_duration_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.max_files < 0 or self.max_output_bytes < 1:
            raise ValueError("Resource limits must be positive")
        if self.max_model_calls < 0 or self.max_duration_seconds <= 0:
            raise ValueError("Resource limits must be positive")


@dataclass(frozen=True)
class AgentRoleSpec:
    """Complete specification of one agent role."""

    role: str
    display: str
    capabilities: tuple[str, ...]
    permissions: tuple[tuple[str, str, str], ...]
    model_requirement: tuple[str, ...]
    task_scope: frozenset[str] = frozenset(TASK_KINDS)
    limits: RoleResourceLimits = field(default_factory=RoleResourceLimits)
    timeout: float = 300.0
    result_schema: dict[str, str] = field(default_factory=dict)
    #: Result fields that MUST be present. Omitting this means every
    #: declared field is required.
    optional_fields: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.role or not self.role.isidentifier():
            raise ValueError(f"Invalid role name: {self.role!r}")
        if not self.capabilities:
            raise ValueError(f"Role {self.role} needs capabilities")
        unknown = [c for c in self.capabilities if c not in ALL_CAPABILITIES]
        if unknown:
            raise ValueError(
                f"Role {self.role} uses unknown capabilities: {unknown}")
        for capability in self.model_requirement:
            if capability not in ALL_CAPABILITIES:
                raise ValueError(
                    f"Role {self.role} model requirement uses unknown "
                    f"capability {capability!r}")
            if capability not in self.capabilities:
                raise ValueError(
                    f"Role {self.role} requires model capability "
                    f"{capability!r} that is not in its own capabilities")
        for resource, operation, scope in self.permissions:
            if not resource or not operation:
                raise ValueError(
                    f"Role {self.role} has a malformed permission entry")
        if not self.task_scope or not self.task_scope <= TASK_KINDS:
            raise ValueError(
                f"Role {self.role} task_scope must be a non-empty subset of "
                f"{sorted(TASK_KINDS)}")
        if self.timeout <= 0:
            raise ValueError(f"Role {self.role} timeout must be positive")
        for name, spec in self.result_schema.items():
            _union(spec)  # raises on unknown type names
            if name in self.optional_fields and \
                    name not in self.result_schema:
                raise ValueError(
                    f"Role {self.role} optional field {name!r} is not "
                    f"declared in result_schema")

    def schema_union(self, name: str) -> tuple:
        return _union(self.result_schema[name])


def validate_result(spec: AgentRoleSpec, result: Any) -> list[str]:
    """Check a structured result against the role's result schema.

    Returns a list of violation strings; an empty list means the result
    conforms. Non-dict results are always a violation for roles with a
    schema, and always valid for roles without one (their output is free
    text).
    """
    if not spec.result_schema:
        return [] if isinstance(result, (str, dict)) else [
            "result must be a dict or text"]
    if not isinstance(result, Mapping):
        return ["result must be a structured dict"]
    violations = []
    for name, spec_str in spec.result_schema.items():
        if name not in result:
            if name in spec.optional_fields:
                continue
            violations.append(f"missing required field {name!r}")
            continue
        value = result[name]
        if isinstance(value, bool) and "bool" not in spec_str:
            violations.append(
                f"field {name!r} expects {spec_str!r}, got bool")
            continue
        if not isinstance(value, spec.schema_union(name)):
            violations.append(
                f"field {name!r} expects {spec_str!r}, got "
                f"{type(value).__name__}")
    return violations


def model_satisfies(spec: AgentRoleSpec,
                    model_capabilities: Mapping[str, Any] | tuple | list) -> bool:
    """Does a model advertise every capability the role requires?

    Accepts either a capability list or a mapping (capability -> truthy
    availability). Missing/absent capabilities fail closed.
    """
    caps = (set(model_capabilities) if isinstance(model_capabilities,
                                                   (tuple, list, set, frozenset))
            else {k for k, v in model_capabilities.items() if v})
    return set(spec.model_requirement) <= caps


PLANNER = AgentRoleSpec(
    role="planner",
    display="Planner",
    capabilities=("planning", "reasoning"),
    permissions=(
        ("filesystem", "read", "**"),
        ("model", "call", "planning"),
    ),
    model_requirement=("planning", "reasoning"),
    task_scope=frozenset({"sequential", "parallel", "dependent"}),
    limits=RoleResourceLimits(max_files=0, max_output_bytes=16 * 1024,
                              max_model_calls=2,
                              max_duration_seconds=120.0),
    timeout=120.0,
    result_schema={"plan": "list", "requirements": "str|list",
                   "risks": "list", "summary": "str"},
    optional_fields=frozenset({"risks", "summary"}),
)

ARCHITECT = AgentRoleSpec(
    role="architect",
    display="Architect",
    capabilities=("planning", "reasoning"),
    permissions=(
        ("filesystem", "read", "**"),
        ("model", "call", "planning"),
    ),
    model_requirement=("planning",),
    task_scope=frozenset({"sequential", "parallel", "dependent"}),
    limits=RoleResourceLimits(max_files=0, max_output_bytes=16 * 1024,
                              max_model_calls=2,
                              max_duration_seconds=180.0),
    timeout=180.0,
    result_schema={"proposal": "list", "risks": "list", "summary": "str"},
    optional_fields=frozenset({"risks", "summary"}),
)

CODER = AgentRoleSpec(
    role="coder",
    display="Coder",
    capabilities=("coding", "tool_use"),
    permissions=(
        ("filesystem", "read", "**"),
        ("filesystem", "write", "**"),
        ("terminal", "execute", "pytest"),
        ("model", "call", "coding"),
    ),
    model_requirement=("coding",),
    task_scope=frozenset({"sequential", "parallel", "dependent"}),
    limits=RoleResourceLimits(max_files=25, max_output_bytes=64 * 1024,
                              max_model_calls=4,
                              max_duration_seconds=600.0),
    timeout=600.0,
    result_schema={"files": "list", "summary": "str"},
    optional_fields=frozenset({"tests_to_run", "risks"}),
)

TESTER = AgentRoleSpec(
    role="tester",
    display="Tester",
    capabilities=("testing",),
    permissions=(
        ("filesystem", "read", "**"),
        ("terminal", "execute", "pytest"),
        ("model", "call", "testing"),
    ),
    model_requirement=(),
    task_scope=frozenset({"sequential", "parallel", "dependent"}),
    limits=RoleResourceLimits(max_files=0, max_output_bytes=32 * 1024,
                              max_model_calls=1,
                              max_duration_seconds=300.0),
    timeout=300.0,
    result_schema={"passed": "bool", "tests_run": "int",
                   "failures": "list", "summary": "str"},
    optional_fields=frozenset({"failures", "summary"}),
)

DEBUGGER = AgentRoleSpec(
    role="debugger",
    display="Debugger",
    capabilities=("debugging", "reasoning"),
    permissions=(
        ("filesystem", "read", "**"),
        ("filesystem", "write", "**"),
        ("terminal", "execute", "pytest"),
        ("model", "call", "debugging"),
    ),
    model_requirement=("debugging",),
    task_scope=frozenset({"sequential", "parallel", "dependent"}),
    limits=RoleResourceLimits(max_files=25, max_output_bytes=64 * 1024,
                              max_model_calls=4,
                              max_duration_seconds=600.0),
    timeout=600.0,
    result_schema={"root_cause": "str", "files": "list", "summary": "str"},
    optional_fields=frozenset({"summary"}),
)

REVIEWER = AgentRoleSpec(
    role="reviewer",
    display="Reviewer",
    capabilities=("review",),
    permissions=(
        ("filesystem", "read", "**"),
        ("git", "diff", ""),
        ("git", "status", ""),
        ("model", "call", "review"),
    ),
    model_requirement=("review",),
    task_scope=frozenset({"sequential", "parallel", "dependent"}),
    limits=RoleResourceLimits(max_files=0, max_output_bytes=32 * 1024,
                              max_model_calls=2,
                              max_duration_seconds=300.0),
    timeout=300.0,
    result_schema={"verdict": "str", "findings": "list", "summary": "str"},
    optional_fields=frozenset({"summary"}),
)

SECURITY = AgentRoleSpec(
    role="security",
    display="Security",
    capabilities=("security",),
    permissions=(
        ("filesystem", "read", "**"),
        ("terminal", "execute", "scan"),
        ("model", "call", "security"),
    ),
    model_requirement=("security",),
    task_scope=frozenset({"sequential", "parallel", "dependent"}),
    limits=RoleResourceLimits(max_files=0, max_output_bytes=32 * 1024,
                              max_model_calls=2,
                              max_duration_seconds=300.0),
    timeout=300.0,
    result_schema={"passed": "bool", "findings": "list", "risk": "str"},
    optional_fields=frozenset({"risk"}),
)

PERFORMANCE = AgentRoleSpec(
    role="performance",
    display="Performance",
    capabilities=("reasoning",),
    permissions=(
        ("filesystem", "read", "**"),
        ("terminal", "execute", "benchmark"),
    ),
    model_requirement=(),
    task_scope=frozenset({"sequential", "parallel", "dependent"}),
    limits=RoleResourceLimits(max_files=0, max_output_bytes=32 * 1024,
                              max_model_calls=0,
                              max_duration_seconds=600.0),
    timeout=600.0,
    result_schema={"measurements": "list", "regressions": "list",
                   "summary": "str"},
    optional_fields=frozenset({"regressions", "summary"}),
)

RESEARCH = AgentRoleSpec(
    role="researcher",
    display="Research",
    capabilities=("research", "reasoning"),
    permissions=(
        ("filesystem", "read", "**"),
        ("model", "call", "research"),
    ),
    model_requirement=("research",),
    task_scope=frozenset({"sequential", "parallel", "dependent"}),
    limits=RoleResourceLimits(max_files=0, max_output_bytes=32 * 1024,
                              max_model_calls=2,
                              max_duration_seconds=300.0),
    timeout=300.0,
    result_schema={"summary": "str", "sources": "list",
                   "hotspots": "list"},
    optional_fields=frozenset({"sources", "hotspots"}),
)

DOCUMENTATION = AgentRoleSpec(
    role="documentation",
    display="Documentation",
    capabilities=("documentation",),
    permissions=(
        ("filesystem", "read", "**"),
        ("filesystem", "write", "docs/**"),
        ("model", "call", "documentation"),
    ),
    model_requirement=("documentation",),
    task_scope=frozenset({"sequential", "parallel", "dependent"}),
    limits=RoleResourceLimits(max_files=25, max_output_bytes=32 * 1024,
                              max_model_calls=2,
                              max_duration_seconds=300.0),
    timeout=300.0,
    result_schema={"files": "list", "summary": "str"},
    optional_fields=frozenset({"coverage"}),
)


#: The ten canonical roles, in canonical order.
AGENT_ROLES: tuple[AgentRoleSpec, ...] = (
    PLANNER, ARCHITECT, CODER, TESTER, DEBUGGER, REVIEWER, SECURITY,
    PERFORMANCE, RESEARCH, DOCUMENTATION,
)

ROLE_SPECS: dict[str, AgentRoleSpec] = {spec.role: spec
                                        for spec in AGENT_ROLES}


def get_role_spec(role: str) -> AgentRoleSpec:
    try:
        return ROLE_SPECS[role]
    except KeyError:
        raise KeyError(f"Unknown agent role: {role!r}") from None


def all_role_specs() -> tuple[AgentRoleSpec, ...]:
    return AGENT_ROLES
