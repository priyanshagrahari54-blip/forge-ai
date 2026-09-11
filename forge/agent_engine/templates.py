"""First-party agent templates (A81).

Every template is a plain specification dictionary. Templates are
*starting points*: they are validated by the same rules as any other
specification, they never grant permissions on their own, and they are
deliberately conservative — writes require approval, network access is
off unless the agent's purpose genuinely requires it, and no template
requests a terminal or commit capability it does not need.
"""
from __future__ import annotations

from typing import Any, Mapping

from forge.agent_engine.spec import AgentSpec, SpecError


def _template(**overrides: Any) -> dict:
    base = {
        "capabilities": ["coding"],
        "tools": [],
        "permissions": {"read_paths": ["**"], "write_paths": [],
                        "require_approval_for_writes": True},
        "model_requirements": {"capability": "coding"},
        "memory_policy": {"scope": "task"},
        "verification": {"required_gates": ["tests", "security"]},
        "resource_limits": {},
    }
    base.update(overrides)
    return base


TEMPLATES: dict = {
    "coding": _template(
        name="coding-agent",
        purpose="Implement and modify repository source code through "
                "reviewed, verified change sets.",
        capabilities=["coding", "debugging", "testing",
                      "structured_output"],
        tools=["read_file", "write_file", "search", "run_tests"],
        permissions={"read_paths": ["**"],
                     "write_paths": ["src/**", "tests/**", "*.py"],
                     "require_approval_for_writes": True},
        model_requirements={"capability": "coding", "complexity": 2.0,
                            "fallback_capability": "reasoning",
                            "max_output_tokens": 8192},
        memory_policy={"scope": "session", "max_entries": 64},
        verification={"required_gates": ["tests", "lint", "review",
                                         "security"],
                      "max_repair_attempts": 2},
        resource_limits={"max_files_touched": 25,
                         "max_tokens_per_run": 40000},
    ),
    "research": _template(
        name="research-agent",
        purpose="Gather, compare, and summarize external and internal "
                "information without modifying the repository.",
        capabilities=["research", "reasoning", "long_context"],
        tools=["read_file", "search", "web_fetch"],
        permissions={"read_paths": ["**"], "write_paths": [],
                     "allow_network": True,
                     "domains": ["docs.python.org", "pypi.org"],
                     "require_approval_for_writes": True},
        model_requirements={"capability": "research",
                            "min_context_window": 32000,
                            "fallback_capability": "reasoning"},
        memory_policy={"scope": "persistent", "max_entries": 200,
                       "ttl_seconds": 86400},
        verification={"required_gates": ["security"],
                      "require_checkpoint": False,
                      "require_independent_review": False},
        resource_limits={"max_files_touched": 0,
                         "max_bytes_written": 0},
    ),
    "security": _template(
        name="security-agent",
        purpose="Audit code and change sets for vulnerabilities, secrets, "
                "and unsafe operations; report findings, never fix "
                "silently.",
        capabilities=["security", "review", "reasoning"],
        tools=["read_file", "search"],
        permissions={"read_paths": ["**"], "write_paths": [],
                     "require_approval_for_writes": True},
        model_requirements={"capability": "security",
                            "fallback_capability": "review"},
        memory_policy={"scope": "session", "max_entries": 128},
        verification={"required_gates": ["security", "review"],
                      "require_checkpoint": False},
        resource_limits={"max_files_touched": 0, "max_bytes_written": 0,
                         "max_runs_per_hour": 60},
    ),
    "gamedev": _template(
        name="game-development-agent",
        purpose="Build and iterate on game systems, assets pipelines, and "
                "gameplay code with deterministic verification.",
        capabilities=["coding", "planning", "testing", "reasoning"],
        tools=["read_file", "write_file", "search", "run_tests"],
        permissions={"read_paths": ["**"],
                     "write_paths": ["game/**", "assets/**", "tests/**"],
                     "require_approval_for_writes": True},
        model_requirements={"capability": "coding", "complexity": 2.5,
                            "fallback_capability": "planning",
                            "max_output_tokens": 8192},
        memory_policy={"scope": "session", "max_entries": 96},
        verification={"required_gates": ["tests", "build", "security"],
                      "max_repair_attempts": 3},
        resource_limits={"max_files_touched": 40,
                         "max_wall_seconds": 900.0},
    ),
    "osdev": _template(
        name="os-development-agent",
        purpose="Work on low-level operating-system code: boot, kernel, "
                "drivers, and toolchain builds, under strict review.",
        capabilities=["coding", "debugging", "reasoning", "planning"],
        tools=["read_file", "write_file", "search", "run_tests", "build"],
        permissions={"read_paths": ["**"],
                     "write_paths": ["kernel/**", "boot/**", "drivers/**"],
                     "allow_terminal": False,
                     "require_approval_for_writes": True},
        model_requirements={"capability": "coding", "complexity": 4.0,
                            "min_context_window": 32000,
                            "fallback_capability": "debugging",
                            "max_output_tokens": 8192},
        memory_policy={"scope": "session", "max_entries": 64},
        verification={"required_gates": ["tests", "build", "review",
                                         "security"],
                      "max_repair_attempts": 3},
        resource_limits={"max_files_touched": 15,
                         "max_wall_seconds": 1200.0,
                         "max_concurrent_runs": 1},
    ),
    "documentation": _template(
        name="documentation-agent",
        purpose="Write and maintain accurate documentation derived from "
                "the actual repository state.",
        capabilities=["documentation", "reasoning", "long_context"],
        tools=["read_file", "write_file", "search"],
        permissions={"read_paths": ["**"],
                     "write_paths": ["docs/**", "README.md"],
                     "require_approval_for_writes": True},
        model_requirements={"capability": "documentation",
                            "fallback_capability": "reasoning",
                            "min_context_window": 16000},
        memory_policy={"scope": "session", "max_entries": 48},
        verification={"required_gates": ["lint", "security"],
                      "require_independent_review": False},
        resource_limits={"max_files_touched": 10,
                         "max_bytes_written": 512 * 1024},
    ),
}

#: Public template names, in stable order.
TEMPLATE_NAMES: tuple = tuple(sorted(TEMPLATES))


def template_payload(name: str) -> dict:
    """Return a deep-ish copy of a template's raw specification dict."""
    key = "".join(ch for ch in str(name or "").lower()
                  if ch.isalnum())
    aliases = {
        "coding": "coding", "codingagent": "coding", "coder": "coding",
        "research": "research", "researchagent": "research",
        "security": "security", "securityagent": "security",
        "gamedev": "gamedev", "gamedevelopment": "gamedev",
        "gamedevelopmentagent": "gamedev", "game": "gamedev",
        "osdev": "osdev", "osdevelopment": "osdev",
        "osdevelopmentagent": "osdev", "os": "osdev",
        "documentation": "documentation", "docs": "documentation",
        "documentationagent": "documentation",
    }
    resolved = aliases.get(key, "")
    if not resolved:
        raise SpecError(
            "Unknown template {0!r}; available: {1}".format(
                name, ", ".join(TEMPLATE_NAMES)))
    payload = TEMPLATES[resolved]
    import copy

    copied = copy.deepcopy(payload)
    copied["template"] = resolved
    return copied


def from_template(name: str, overrides: Mapping = None) -> AgentSpec:
    """Build a validated :class:`AgentSpec` from a named template.

    ``overrides`` are shallow-merged over the template payload and are
    re-validated. Overrides can never bypass validation, and they can
    never introduce a forbidden tool or a secret-bearing memory policy.
    """
    payload = template_payload(name)
    for key, value in dict(overrides or {}).items():
        payload[key] = value
    return AgentSpec.from_dict(payload)


def all_specs() -> list:
    """Every first-party template as a validated spec (self-check)."""
    return [from_template(name) for name in TEMPLATE_NAMES]
