"""Built-in agent templates (A81).

Each template is a complete, least-privilege starting specification:
tools and their implied permissions only, bounded memory, a benchmark
gate, and resource limits with confined working directories. Templates
are first-party (shipped with Forge) but produce ordinary agents —
nothing about a templated agent bypasses the lifecycle, the PolicyGate,
or the no-self-grant rule.
"""
from __future__ import annotations

from typing import Any

from forge.agent_engine.errors import SpecError
from forge.agent_engine.spec import (
    TOOL_PERMISSIONS,
    AgentSpec,
    MemoryPolicy,
    ModelRequirements,
    ResourceLimits,
    VerificationRequirements,
)

TEMPLATE_IDS: tuple[str, ...] = (
    "coding",
    "research",
    "security",
    "game-dev",
    "os-dev",
    "documentation",
)

TEMPLATE_DESCRIPTIONS: dict[str, str] = {
    "coding": "Implements and repairs code with tests, bounded writes, "
              "and review-grade verification.",
    "research": "Reads and searches the repository and answers questions; "
                "read-only tools only.",
    "security": "Audits code for secrets and dangerous patterns; read-only "
                "plus tests, with mandatory security review.",
    "game-dev": "Builds and tests game code and assets inside src/assets/"
                "scenes/tests.",
    "os-dev": "Develops low-level and kernel code; may run approved build "
               "and test commands inside its working directories.",
    "documentation": "Writes and maintains documentation in docs/.",
}


def _spec_from(*, tools: tuple[str, ...], capabilities: tuple[str, ...],
               model_capabilities: tuple[str, ...],
               benchmark: str, working_dirs: tuple[str, ...],
               max_requests: int = 200,
               min_context: int = 4096,
               security_review: bool = False) -> dict[str, Any]:
    return {
        "capabilities": list(capabilities),
        "tools": list(tools),
        "permissions": list(dict.fromkeys(
            TOOL_PERMISSIONS[tool] for tool in tools)),
        "model": {
            "capabilities": list(model_capabilities),
            "preference": "any",
            "min_context_tokens": min_context,
            "allow_fallback": True,
        },
        "memory": {
            "enabled": True,
            "max_entries": 500,
            "max_entry_bytes": 64 * 1024,
            "retention": "version",
        },
        "verification": {
            "benchmark": benchmark,
            "min_score": 0.75,
            "min_scenarios": 0,
            "tests_required": True,
            "security_review": security_review,
        },
        "limits": {
            "max_tokens_per_request": 8192,
            "max_requests": max_requests,
            "max_wall_seconds": 1800.0,
            "max_files_written": 50,
            "working_dirs": list(working_dirs),
        },
    }


_TEMPLATE_FIELDS: dict[str, dict[str, Any]] = {
    "coding": _spec_from(
        tools=("read_file", "write_file", "run_tests", "search",
               "git_status", "git_diff"),
        capabilities=("coding", "debugging", "testing"),
        model_capabilities=("coding", "debugging"),
        benchmark="coding",
        working_dirs=("src", "tests", "lib"),
    ),
    "research": _spec_from(
        tools=("read_file", "search", "git_status"),
        capabilities=("research", "reasoning"),
        model_capabilities=("research", "reasoning"),
        benchmark="research",
        working_dirs=(),
        min_context=8192,
    ),
    "security": _spec_from(
        tools=("read_file", "search", "git_status", "git_diff",
               "run_tests"),
        capabilities=("security", "review", "reasoning"),
        model_capabilities=("security", "review", "reasoning"),
        benchmark="security",
        working_dirs=(),
        security_review=True,
    ),
    "game-dev": _spec_from(
        tools=("read_file", "write_file", "run_tests", "search",
               "git_status"),
        capabilities=("coding", "testing", "planning", "reasoning"),
        model_capabilities=("coding", "testing"),
        benchmark="game-dev",
        working_dirs=("src", "assets", "scenes", "tests"),
    ),
    "os-dev": _spec_from(
        tools=("read_file", "write_file", "run_tests", "search",
               "git_status", "git_diff", "run_command"),
        capabilities=("coding", "debugging", "reasoning"),
        model_capabilities=("coding", "debugging", "reasoning"),
        benchmark="os-dev",
        working_dirs=("src", "kernel", "scripts", "tests"),
    ),
    "documentation": _spec_from(
        tools=("read_file", "write_file", "search", "git_status"),
        capabilities=("documentation", "review"),
        model_capabilities=("documentation", "review"),
        benchmark="documentation",
        working_dirs=("docs",),
    ),
}

TEMPLATE_PURPOSES: dict[str, str] = {
    "coding": "Implement, fix, and test repository code within the "
              "declared working directories.",
    "research": "Answer questions about the repository from its code and "
                "history without modifying anything.",
    "security": "Audit repository code for secrets, dangerous patterns, "
                "and unsafe changes.",
    "game-dev": "Build and test game code and assets inside the project's "
                "game directories.",
    "os-dev": "Develop and test low-level operating-system code with "
              "approved build commands.",
    "documentation": "Write, review, and maintain the project's "
                     "documentation.",
}


def build_template_spec(template: str, name: str, *,
                        purpose: str = "") -> AgentSpec:
    """Build a validated :class:`AgentSpec` for one of the templates."""
    template = (template or "").strip().lower()
    if template not in TEMPLATE_IDS:
        raise SpecError(
            f"unknown template {template!r}; available: "
            f"{', '.join(TEMPLATE_IDS)}")
    fields = _TEMPLATE_FIELDS[template]
    payload: dict[str, Any] = {
        "name": name,
        "purpose": (purpose or "").strip() or TEMPLATE_PURPOSES[template],
        **fields,
    }
    return AgentSpec.from_dict(payload)


def template_catalog() -> list[dict[str, Any]]:
    """Describe every built-in template for the CLI and desktop UI."""
    catalog: list[dict[str, Any]] = []
    for template in TEMPLATE_IDS:
        fields = _TEMPLATE_FIELDS[template]
        catalog.append({
            "id": template,
            "description": TEMPLATE_DESCRIPTIONS[template],
            "purpose": TEMPLATE_PURPOSES[template],
            "tools": list(fields["tools"]),
            "permissions": list(fields["permissions"]),
            "capabilities": list(fields["capabilities"]),
            "benchmark": fields["verification"]["benchmark"],
            "working_dirs": list(fields["limits"]["working_dirs"]),
        })
    return catalog
