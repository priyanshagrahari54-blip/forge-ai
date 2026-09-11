"""Agent templates (A81): reviewed starting points, never capabilities.

A template is a pre-reviewed specification skeleton for a common agent
kind. Instantiating one copies its fields into a brand-new spec — the
result is an ordinary package that still has to be validated, tested,
and enabled before it can run anything. A template grants nothing by
itself.
"""
from __future__ import annotations

from typing import Any, Mapping

from forge.agents.engine.spec import (
    AgentSpecification,
    MemoryPolicy,
    ModelRequirements,
    ResourceLimits,
    VerificationRequirements,
)

TEMPLATE_NAMES = (
    "coding",
    "research",
    "security",
    "game-dev",
    "os-dev",
    "documentation",
)

#: Every template's tools must be covered by its permissions (the spec
#: validator enforces this; the tables below are written to comply).
TEMPLATES: dict[str, dict[str, Any]] = {
    "coding": {
        "label": "Coding Agent",
        "description": "Implements and repairs code through permissioned "
                       "change sets, tests, and review.",
        "purpose": "Implement, modify, and repair repository code through "
                   "validated change sets, bounded test runs, and "
                   "independent review.",
        "capabilities": ("coding", "debugging", "testing", "review"),
        "tools": ("read_file", "search", "write_file", "run_tests",
                  "git_status", "git_diff"),
        "permissions": ("filesystem:read", "filesystem:write",
                        "terminal:execute", "git:status", "git:diff"),
        "model_requirements": ModelRequirements(
            capabilities=("coding",),
            min_context_tokens=0,
            prefer_local=True,
            allow_remote=True,
            allow_paid=False,
            routing_policy="balanced"),
        "memory_policy": MemoryPolicy(
            enabled=True, scope="agent", max_entries=100,
            share_across_agents=False),
        "verification": VerificationRequirements(
            run_tests=True, run_review=True, run_security=True,
            require_all=True),
        "resource_limits": ResourceLimits(
            max_runs_per_hour=20, max_concurrent=2,
            max_tool_calls_per_run=40, max_output_tokens=8192,
            timeout_seconds=900.0),
    },
    "research": {
        "label": "Research Agent",
        "description": "Read-only repository and knowledge research; "
                       "never writes code.",
        "purpose": "Investigate the repository and gathered context to "
                   "answer questions with evidence; read-only by design.",
        "capabilities": ("research", "reasoning", "documentation"),
        "tools": ("read_file", "search", "git_status"),
        "permissions": ("filesystem:read", "git:status"),
        "model_requirements": ModelRequirements(
            capabilities=("research",),
            min_context_tokens=0,
            prefer_local=True,
            allow_remote=True,
            allow_paid=False,
            routing_policy="quality"),
        "memory_policy": MemoryPolicy(
            enabled=True, scope="agent", max_entries=200,
            share_across_agents=False),
        "verification": VerificationRequirements(
            run_tests=False, run_review=True, run_security=False,
            require_all=True),
        "resource_limits": ResourceLimits(
            max_runs_per_hour=30, max_concurrent=3,
            max_tool_calls_per_run=25, max_output_tokens=4096,
            timeout_seconds=600.0),
    },
    "security": {
        "label": "Security Agent",
        "description": "Audit-focused: scans code and diffs for secrets, "
                       "dangerous operations, and unsafe paths.",
        "purpose": "Audit code and proposed changes for secrets, "
                   "dangerous execution, traversal, and unsafe paths; "
                   "report findings without modifying the tree.",
        "capabilities": ("security", "review", "reasoning"),
        "tools": ("read_file", "search", "git_status", "git_diff"),
        "permissions": ("filesystem:read", "git:status", "git:diff"),
        "model_requirements": ModelRequirements(
            capabilities=("security",),
            min_context_tokens=0,
            prefer_local=True,
            allow_remote=True,
            allow_paid=False,
            routing_policy="quality"),
        "memory_policy": MemoryPolicy(
            enabled=True, scope="agent", max_entries=150,
            share_across_agents=False),
        "verification": VerificationRequirements(
            run_tests=False, run_review=True, run_security=True,
            require_all=True),
        "resource_limits": ResourceLimits(
            max_runs_per_hour=15, max_concurrent=2,
            max_tool_calls_per_run=30, max_output_tokens=8192,
            timeout_seconds=900.0),
    },
    "game-dev": {
        "label": "Game Development Agent",
        "description": "Builds gameplay systems with asset-aware, "
                       "test-verified change sets.",
        "purpose": "Implement gameplay mechanics, physics, and rendering "
                   "systems with verifiable behavior and protected asset "
                   "boundaries.",
        "capabilities": ("coding", "reasoning", "testing"),
        "tools": ("read_file", "search", "write_file", "run_tests",
                  "git_status"),
        "permissions": ("filesystem:read", "filesystem:write",
                        "terminal:execute", "git:status"),
        "model_requirements": ModelRequirements(
            capabilities=("coding", "reasoning"),
            min_context_tokens=0,
            prefer_local=True,
            allow_remote=True,
            allow_paid=False,
            routing_policy="balanced"),
        "memory_policy": MemoryPolicy(
            enabled=True, scope="agent", max_entries=120,
            share_across_agents=False),
        "verification": VerificationRequirements(
            run_tests=True, run_review=True, run_security=True,
            require_all=True),
        "resource_limits": ResourceLimits(
            max_runs_per_hour=12, max_concurrent=2,
            max_tool_calls_per_run=35, max_output_tokens=8192,
            timeout_seconds=1200.0),
    },
    "os-dev": {
        "label": "OS Development Agent",
        "description": "Kernel/systems work: conservative writes, "
                       "strict verification, low limits.",
        "purpose": "Develop operating-system components — kernels, "
                   "drivers, boot code — with conservative change sets "
                   "and strict verification before acceptance.",
        "capabilities": ("coding", "debugging", "testing"),
        "tools": ("read_file", "search", "write_file", "run_tests",
                  "git_status", "git_diff"),
        "permissions": ("filesystem:read", "filesystem:write",
                        "terminal:execute", "git:status", "git:diff"),
        "model_requirements": ModelRequirements(
            capabilities=("coding", "debugging"),
            min_context_tokens=0,
            prefer_local=True,
            allow_remote=True,
            allow_paid=False,
            routing_policy="quality"),
        "memory_policy": MemoryPolicy(
            enabled=True, scope="agent", max_entries=100,
            share_across_agents=False),
        "verification": VerificationRequirements(
            run_tests=True, run_review=True, run_security=True,
            require_all=True),
        "resource_limits": ResourceLimits(
            max_runs_per_hour=8, max_concurrent=1,
            max_tool_calls_per_run=25, max_output_tokens=8192,
            timeout_seconds=1800.0),
    },
    "documentation": {
        "label": "Documentation Agent",
        "description": "Writes and maintains docs, READMEs, and API "
                       "references from real code.",
        "purpose": "Write and maintain accurate documentation derived "
                   "from the actual codebase — READMEs, guides, and API "
                   "references that stay truthful.",
        "capabilities": ("documentation", "research"),
        "tools": ("read_file", "search", "write_file", "git_status"),
        "permissions": ("filesystem:read", "filesystem:write",
                        "git:status"),
        "model_requirements": ModelRequirements(
            capabilities=("documentation",),
            min_context_tokens=0,
            prefer_local=True,
            allow_remote=True,
            allow_paid=False,
            routing_policy="balanced"),
        "memory_policy": MemoryPolicy(
            enabled=True, scope="agent", max_entries=150,
            share_across_agents=False),
        "verification": VerificationRequirements(
            run_tests=False, run_review=True, run_security=True,
            require_all=True),
        "resource_limits": ResourceLimits(
            max_runs_per_hour=25, max_concurrent=2,
            max_tool_calls_per_run=20, max_output_tokens=8192,
            timeout_seconds=600.0),
    },
}


def template_names() -> list[str]:
    return list(TEMPLATE_NAMES)


def template_summaries() -> list[dict[str, Any]]:
    return [
        {
            "template": name,
            "label": body["label"],
            "description": body["description"],
            "capabilities": list(body["capabilities"]),
            "tools": list(body["tools"]),
            "permissions": list(body["permissions"]),
        }
        for name, body in ((key, TEMPLATES[key]) for key in TEMPLATE_NAMES)
    ]


def get_template(name: str) -> dict[str, Any]:
    key = (name or "").strip().lower()
    if key not in TEMPLATES:
        raise ValueError(
            f"Unknown template {name!r}; choose from "
            f"{', '.join(TEMPLATE_NAMES)}")
    return TEMPLATES[key]


def spec_from_template(template: str, *, name: str, purpose: str = "",
                       overrides: Mapping[str, Any] | None = None,
                       ) -> AgentSpecification:
    """Build a specification from a template plus explicit overrides.

    Only the operator-provided ``name`` (required) and ``purpose``
    (default: the template purpose) come from outside; every override
    is re-validated by the specification's own rules.
    """
    body = get_template(template)
    overrides = dict(overrides or {})
    purpose = (purpose or overrides.pop("purpose", "") or
               body["purpose"]).strip()
    capabilities = tuple(overrides.pop("capabilities",
                                       body["capabilities"]))
    tools = tuple(overrides.pop("tools", body["tools"]))
    permissions = tuple(overrides.pop("permissions",
                                      body["permissions"]))
    model_requirements = ModelRequirements.from_dict(
        overrides.pop("model_requirements",
                      body["model_requirements"].to_dict()))
    memory_policy = MemoryPolicy.from_dict(
        overrides.pop("memory_policy",
                      body["memory_policy"].to_dict()))
    verification = VerificationRequirements.from_dict(
        overrides.pop("verification", body["verification"].to_dict()))
    resource_limits = ResourceLimits.from_dict(
        overrides.pop("resource_limits",
                      body["resource_limits"].to_dict()))
    if overrides:
        raise ValueError(
            f"Unknown template override keys: {sorted(overrides)}")
    spec = AgentSpecification(
        name=name,
        purpose=purpose,
        capabilities=capabilities,
        tools=tools,
        permissions=permissions,
        model_requirements=model_requirements,
        memory_policy=memory_policy,
        verification=verification,
        resource_limits=resource_limits,
        template=template,
    )
    spec.validate()
    return spec
