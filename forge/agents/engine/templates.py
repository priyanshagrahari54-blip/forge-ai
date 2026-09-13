"""Agent templates (A82): six first-party starting specifications.

A template is a *specification*, not a shortcut around validation: it
produces an :class:`~forge.agents.engine.spec.AgentSpec` that goes through
exactly the same checks as a hand-written one, and it grants nothing — the
operator still has to record grants and move the agent through
``validated`` → ``tested`` → ``enabled``.

The templates differ where it matters:

* **coding** / **game-development** / **os-development** may write files
  (and os-development may run an approved command); all three require
  security scanning and review, and coding/os-development require the
  project test suite to pass.
* **research** / **security** are read-only: their mode ceiling is
  ``safe``, so a write is refused by the session mode as well as by the
  absence of any write tool.
* **documentation** may write, but only under ``docs/`` and
  ``README.md``.

``capabilities`` describe what the *agent* advertises; ``model``
requirements describe what the *model* must support for routing. They are
deliberately separate — an agent can advertise ``image_generation`` while
routing on ``coding``, rather than demanding a multimodal model it may not
have.
"""
from __future__ import annotations

from typing import Any

from forge.agents.engine.errors import AgentSpecError
from forge.agents.engine.spec import (
    AgentSpec,
    MemoryPolicy,
    ModelRequirements,
    PermissionSpec,
    ResourceLimits,
    ToolGrant,
    VerificationSpec,
    validate_spec,
)

CODING = "coding"
RESEARCH = "research"
SECURITY = "security"
GAME_DEVELOPMENT = "game-development"
OS_DEVELOPMENT = "os-development"
DOCUMENTATION = "documentation"

TEMPLATES: dict = {
    CODING: {
        "title": "Coding Agent",
        "description": "Implements and repairs project code, then proves it "
                       "with the project test suite.",
        "role": "coding",
        "capabilities": ("coding", "reasoning", "debugging", "testing"),
        "tools": (("read_file", 16), ("search", 8), ("git_status", 4),
                  ("run_tests", 4), ("write_file", 12)),
        "operations": ("read_file", "search_files", "git_status",
                       "run_tests", "write_file"),
        "mode_ceiling": "assisted",
        "allowed_paths": (),
        "denied_paths": ("secrets/",),
        "model": {"capabilities": ("coding",), "min_context_window": 4096},
        "memory": {"scope": "agent", "max_entries": 64},
        "verification": {"require_security_scan": True,
                         "require_tests": True, "require_review": True},
        "limits": {"max_runs_per_hour": 20, "max_model_calls": 6,
                   "max_file_writes": 12, "max_wall_seconds": 300.0},
    },
    RESEARCH: {
        "title": "Research Agent",
        "description": "Reads and summarises the project and its "
                       "dependencies; never writes.",
        "role": "research",
        "capabilities": ("research", "reasoning", "long_context"),
        "tools": (("read_file", 32), ("search", 16), ("git_status", 4)),
        "operations": ("read_file", "search_files", "git_status"),
        "mode_ceiling": "safe",
        "allowed_paths": (),
        "denied_paths": (),
        "model": {"capabilities": ("research", "long_context"),
                  "min_context_window": 8192},
        "memory": {"scope": "project", "max_entries": 128},
        "verification": {"require_security_scan": True},
        "limits": {"max_runs_per_hour": 40, "max_model_calls": 6,
                   "max_file_writes": 1, "max_wall_seconds": 300.0},
    },
    SECURITY: {
        "title": "Security Agent",
        "description": "Audits the project for secrets, dangerous calls, "
                       "and unsafe paths; read-only by design.",
        "role": "security",
        "capabilities": ("security", "review", "reasoning"),
        "tools": (("read_file", 32), ("search", 16), ("run_tests", 4)),
        "operations": ("read_file", "search_files", "run_tests"),
        "mode_ceiling": "safe",
        "allowed_paths": (),
        "denied_paths": (),
        "model": {"capabilities": ("security",), "min_context_window": 4096},
        "memory": {"scope": "agent", "max_entries": 64},
        "verification": {"require_security_scan": True,
                         "require_review": True},
        "limits": {"max_runs_per_hour": 30, "max_model_calls": 6,
                   "max_file_writes": 1, "max_wall_seconds": 300.0},
    },
    GAME_DEVELOPMENT: {
        "title": "Game Development Agent",
        "description": "Builds gameplay code and asset manifests, keeping "
                       "generated build output out of the tree.",
        "role": "game-development",
        "capabilities": ("coding", "reasoning", "image_generation"),
        "tools": (("read_file", 16), ("search", 8), ("run_tests", 4),
                  ("write_file", 16)),
        "operations": ("read_file", "search_files", "run_tests",
                       "write_file"),
        "mode_ceiling": "assisted",
        "allowed_paths": (),
        "denied_paths": ("build/", "dist/"),
        "model": {"capabilities": ("coding",), "min_context_window": 8192},
        "memory": {"scope": "agent", "max_entries": 96},
        "verification": {"require_security_scan": True,
                         "require_tests": True},
        "limits": {"max_runs_per_hour": 20, "max_model_calls": 6,
                   "max_file_writes": 16, "max_wall_seconds": 600.0},
    },
    OS_DEVELOPMENT: {
        "title": "OS Development Agent",
        "description": "Works on low-level system code with an approved "
                       "command path and the strictest verification.",
        "role": "os-development",
        "capabilities": ("coding", "reasoning", "debugging"),
        "tools": (("read_file", 16), ("search", 8), ("git_status", 4),
                  ("run_tests", 4), ("write_file", 10), ("terminal", 2)),
        "operations": ("read_file", "search_files", "git_status",
                       "run_tests", "write_file", "run_command"),
        "mode_ceiling": "assisted",
        "allowed_paths": (),
        "denied_paths": (".github/", "secrets/"),
        "model": {"capabilities": ("coding", "debugging"),
                  "min_context_window": 8192},
        "memory": {"scope": "agent", "max_entries": 64},
        "verification": {"require_security_scan": True,
                         "require_tests": True, "require_review": True},
        "limits": {"max_runs_per_hour": 10, "max_model_calls": 6,
                   "max_file_writes": 12, "max_wall_seconds": 600.0},
    },
    DOCUMENTATION: {
        "title": "Documentation Agent",
        "description": "Writes and maintains docs; may only touch docs/ "
                       "and README.md.",
        "role": "documentation",
        "capabilities": ("documentation", "reasoning"),
        "tools": (("read_file", 24), ("search", 12), ("write_file", 16)),
        "operations": ("read_file", "search_files", "write_file"),
        "mode_ceiling": "assisted",
        "allowed_paths": ("docs/", "README.md"),
        "denied_paths": (),
        "model": {"capabilities": ("documentation",),
                  "min_context_window": 4096},
        "memory": {"scope": "project", "max_entries": 96},
        "verification": {"require_security_scan": True,
                         "require_review": True},
        "limits": {"max_runs_per_hour": 30, "max_model_calls": 4,
                   "max_file_writes": 16, "max_wall_seconds": 300.0},
    },
}

DEFAULT_PURPOSES: dict = {
    template: payload["description"]
    for template, payload in TEMPLATES.items()
}


def template_ids() -> tuple:
    return tuple(TEMPLATES)


def describe_templates() -> list:
    """Template metadata for the CLI and the desktop Agent Manager."""
    return [{
        "id": template_id,
        "title": payload["title"],
        "description": payload["description"],
        "role": payload["role"],
        "capabilities": list(payload["capabilities"]),
        "tools": [name for name, _max in payload["tools"]],
        "operations": list(payload["operations"]),
        "mode_ceiling": payload["mode_ceiling"],
        "allowed_paths": list(payload["allowed_paths"]),
        "denied_paths": list(payload["denied_paths"]),
        "verification": dict(payload["verification"]),
        "limits": dict(payload["limits"]),
    } for template_id, payload in sorted(TEMPLATES.items())]


def spec_from_template(template_id: str, *, name: str, purpose: str = "",
                       overrides: Any = None) -> AgentSpec:
    """Build (and validate) a spec from a first-party template.

    ``overrides`` may replace whole sections (``tools``, ``permissions``,
    ``model``, ``memory``, ``verification``, ``limits``, ``capabilities``,
    ``tags``). Overrides are re-validated, so a template cannot be used to
    smuggle an invalid spec past the factory.
    """
    key = (template_id or "").strip().lower()
    payload = TEMPLATES.get(key)
    if payload is None:
        raise AgentSpecError(
            "Unknown template %r (available: %s)"
            % (template_id, ", ".join(sorted(TEMPLATES))))
    document: dict = {
        "name": name,
        "purpose": (purpose or "").strip() or DEFAULT_PURPOSES[key],
        "role": payload["role"],
        "template": key,
        "capabilities": list(payload["capabilities"]),
        "tools": [{"name": tool_name, "max_calls": max_calls}
                  for tool_name, max_calls in payload["tools"]],
        "permissions": {
            "operations": list(payload["operations"]),
            "mode_ceiling": payload["mode_ceiling"],
            "allowed_paths": list(payload["allowed_paths"]),
            "denied_paths": list(payload["denied_paths"]),
        },
        "model": dict(payload["model"]),
        "memory": dict(payload["memory"]),
        "verification": dict(payload["verification"]),
        "limits": dict(payload["limits"]),
    }
    if isinstance(overrides, dict):
        for section in ("capabilities", "tools", "permissions", "model",
                        "memory", "verification", "limits", "tags", "role"):
            if section in overrides:
                document[section] = overrides[section]
    return validate_spec(document)


def default_spec(template_id: str, name: str) -> AgentSpec:
    """Convenience wrapper used by the CLI's ``--template`` path."""
    return spec_from_template(template_id, name=name)


def is_template(template_id: str) -> bool:
    return (template_id or "").strip().lower() in TEMPLATES


def tool_grants(pairs: Any) -> tuple:
    """``((name, max_calls), ...)`` → validated :class:`ToolGrant` tuple."""
    return tuple(ToolGrant(name=str(name), max_calls=int(max_calls))
                 for name, max_calls in pairs)


def build_spec(*, name: str, purpose: str, capabilities: Any,
               tools: Any = (), operations: Any = (),
               mode_ceiling: str = "assisted",
               allowed_paths: Any = (), denied_paths: Any = (),
               model: Any = None, memory: Any = None,
               verification: Any = None, limits: Any = None,
               role: str = "", tags: Any = ()) -> AgentSpec:
    """Assemble a spec from explicit parts (used by tests and the API)."""
    return AgentSpec(
        name=name, purpose=purpose, capabilities=tuple(capabilities),
        tools=tuple(tools),
        permissions=PermissionSpec(operations=tuple(operations),
                                   mode_ceiling=mode_ceiling,
                                   allowed_paths=tuple(allowed_paths),
                                   denied_paths=tuple(denied_paths)),
        model=ModelRequirements(**(model or {})),
        memory=MemoryPolicy(**(memory or {})),
        verification=VerificationSpec(**(verification or {})),
        limits=ResourceLimits(**(limits or {})),
        role=role, tags=tuple(tags))
