"""First-party agent templates for the Creation Engine.

Six curated starting points — each a complete nine-dimension spec with
sensible tools, permissions, model, memory, verification, and resource
defaults. Templates are data, not power: instantiating one still goes
through the full create -> validate -> test -> enable lifecycle, and
every requested permission still needs operator approval.
"""
from __future__ import annotations

from typing import Any

TEMPLATE_NAMES: tuple[str, ...] = (
    "coding",
    "research",
    "security",
    "game-dev",
    "os-dev",
    "documentation",
)


def _base(*, capabilities: list[str], tools: list[str],
          permissions: list[dict[str, Any]],
          model_capability: str,
          verification: list[str],
          max_runs_per_hour: int = 60,
          max_concurrent: int = 2) -> dict[str, Any]:
    return {
        "capabilities": capabilities,
        "tools": tools,
        "permissions": permissions,
        "model_requirements": {
            "capability": model_capability,
            "prefer_local": True,
            "prefer_free": True,
            "min_context": 0,
            "models": [],
        },
        "memory_policy": {
            "max_entries": 200,
            "max_value_chars": 2000,
            "isolated": True,
        },
        "verification_requirements": verification,
        "resource_limits": {
            "max_runs_per_hour": max_runs_per_hour,
            "max_concurrent": max_concurrent,
            "max_seconds": 300,
        },
    }


_TEMPLATES: dict[str, dict[str, Any]] = {
    "coding": {
        **_base(
            capabilities=["coding", "debugging", "testing"],
            tools=["read_file", "write_file", "search", "run_tests",
                   "git_status", "git_diff", "memory_read", "memory_write"],
            permissions=[
                {"resource": "filesystem", "operation": "read",
                 "scope": "**", "effect": "ALLOW"},
                {"resource": "filesystem", "operation": "write",
                 "scope": "src/**", "effect": "REQUIRE_APPROVAL"},
                {"resource": "terminal", "operation": "execute",
                 "scope": "pytest", "effect": "REQUIRE_APPROVAL"},
                {"resource": "memory", "operation": "read",
                 "scope": "", "effect": "ALLOW"},
                {"resource": "memory", "operation": "write",
                 "scope": "", "effect": "ALLOW"},
            ],
            model_capability="coding",
            verification=["tests", "build", "security", "review"],
        ),
        "purpose": ("Implements, debugs, and tests project code through "
                    "the gated ChangeSet engine."),
    },
    "research": {
        **_base(
            capabilities=["research", "planning"],
            tools=["read_file", "search", "git_status", "browser",
                   "memory_read", "memory_write"],
            permissions=[
                {"resource": "filesystem", "operation": "read",
                 "scope": "**", "effect": "ALLOW"},
                {"resource": "browser", "operation": "read",
                 "scope": "example.com", "effect": "REQUIRE_APPROVAL"},
                {"resource": "memory", "operation": "read",
                 "scope": "", "effect": "ALLOW"},
                {"resource": "memory", "operation": "write",
                 "scope": "", "effect": "ALLOW"},
            ],
            model_capability="research",
            verification=["tests", "security"],
        ),
        "purpose": ("Surveys repositories and sources, then reports "
                    "structured findings. Read-only by default."),
    },
    "security": {
        **_base(
            capabilities=["security", "review"],
            tools=["read_file", "search", "git_status", "git_diff",
                   "memory_read", "memory_write"],
            permissions=[
                {"resource": "filesystem", "operation": "read",
                 "scope": "**", "effect": "ALLOW"},
                {"resource": "memory", "operation": "read",
                 "scope": "", "effect": "ALLOW"},
                {"resource": "memory", "operation": "write",
                 "scope": "", "effect": "ALLOW"},
            ],
            model_capability="security",
            verification=["tests", "security", "review"],
        ),
        "purpose": ("Audits code for secrets, dangerous patterns, and "
                    "unsafe dependencies. Never writes."),
    },
    "game-dev": {
        **_base(
            capabilities=["coding", "testing", "planning"],
            tools=["read_file", "write_file", "search", "run_tests",
                   "git_status", "memory_read", "memory_write"],
            permissions=[
                {"resource": "filesystem", "operation": "read",
                 "scope": "**", "effect": "ALLOW"},
                {"resource": "filesystem", "operation": "write",
                 "scope": "game/**", "effect": "REQUIRE_APPROVAL"},
                {"resource": "terminal", "operation": "execute",
                 "scope": "pytest", "effect": "REQUIRE_APPROVAL"},
                {"resource": "memory", "operation": "read",
                 "scope": "", "effect": "ALLOW"},
                {"resource": "memory", "operation": "write",
                 "scope": "", "effect": "ALLOW"},
            ],
            model_capability="coding",
            verification=["tests", "build", "security"],
        ),
        "purpose": ("Builds game systems, scenes, and gameplay logic with "
                    "bounded, test-covered changes."),
    },
    "os-dev": {
        **_base(
            capabilities=["coding", "debugging", "testing"],
            tools=["read_file", "write_file", "search", "run_tests",
                   "git_status", "git_diff", "memory_read", "memory_write"],
            permissions=[
                {"resource": "filesystem", "operation": "read",
                 "scope": "**", "effect": "ALLOW"},
                {"resource": "filesystem", "operation": "write",
                 "scope": "kernel/**", "effect": "REQUIRE_APPROVAL"},
                {"resource": "terminal", "operation": "execute",
                 "scope": "pytest", "effect": "REQUIRE_APPROVAL"},
                {"resource": "memory", "operation": "read",
                 "scope": "", "effect": "ALLOW"},
                {"resource": "memory", "operation": "write",
                 "scope": "", "effect": "ALLOW"},
            ],
            model_capability="coding",
            verification=["tests", "build", "security", "review"],
            max_runs_per_hour=30,
            max_concurrent=1,
        ),
        "purpose": ("Develops operating-system components with strict "
                    "verification and conservative quotas."),
    },
    "documentation": {
        **_base(
            capabilities=["documentation", "research"],
            tools=["read_file", "write_file", "search", "git_status",
                   "memory_read", "memory_write"],
            permissions=[
                {"resource": "filesystem", "operation": "read",
                 "scope": "**", "effect": "ALLOW"},
                {"resource": "filesystem", "operation": "write",
                 "scope": "docs/**", "effect": "REQUIRE_APPROVAL"},
                {"resource": "memory", "operation": "read",
                 "scope": "", "effect": "ALLOW"},
                {"resource": "memory", "operation": "write",
                 "scope": "", "effect": "ALLOW"},
            ],
            model_capability="documentation",
            verification=["tests", "security"],
        ),
        "purpose": ("Writes and maintains docs scoped to docs/**. "
                    "Cannot touch source outside docs."),
    },
}


def template_names() -> list[str]:
    return list(TEMPLATE_NAMES)


def get_template(name: str) -> dict[str, Any]:
    """Return a deep copy of the named template spec fields."""
    import copy

    key = (name or "").strip().lower()
    if key not in _TEMPLATES:
        raise ValueError(
            f"Unknown template {name!r}; expected one of "
            f"{list(TEMPLATE_NAMES)}")
    return copy.deepcopy(_TEMPLATES[key])


def build_spec(template: str, name: str, purpose: str = "") -> dict[str, Any]:
    """Build a full spec dict from a template + identity.

    ``purpose`` overrides the template default when non-empty.
    """
    fields = get_template(template)
    fields["name"] = (name or "").strip().lower()
    if (purpose or "").strip():
        fields["purpose"] = purpose.strip()
    return fields


def list_templates() -> list[dict[str, Any]]:
    """Describe every template (name, purpose, capabilities, tools)."""
    items = []
    for name in TEMPLATE_NAMES:
        fields = _TEMPLATES[name]
        items.append({
            "name": name,
            "purpose": fields["purpose"],
            "capabilities": list(fields["capabilities"]),
            "tools": list(fields["tools"]),
            "verification_requirements": list(
                fields["verification_requirements"]),
        })
    return items
