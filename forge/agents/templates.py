"""First-party agent templates (Forge Agent Creation Engine).

Six curated starting points — coding, research, security, game
development, OS development, documentation. Each template builds a
fully validated :class:`AgentSpec`; callers only supply the agent
name (and optionally override the purpose).
"""
from __future__ import annotations

from typing import Any, Callable

from forge.agents.spec import (AgentSpec, MemoryPolicy, ModelRequirements,
                               ResourceLimits, VerificationRequirements)

TEMPLATE_NAMES = (
    "coding",
    "research",
    "security",
    "game-development",
    "os-development",
    "documentation",
)


def coding_template(name: str, purpose: str = "") -> AgentSpec:
    return AgentSpec(
        name=name,
        purpose=purpose or (
            "Implement, fix, and refactor project code through "
            "gated change sets with tests and review."),
        capabilities=("coding", "debugging", "testing", "review"),
        tools=("read_file", "write_file", "run_tests", "search",
               "git_status", "git_diff", "terminal"),
        permissions=("read_file", "search_files", "write_file",
                     "run_tests", "run_command", "git_status",
                     "git_diff"),
        model_requirements=ModelRequirements(
            ("coding", "debugging", "testing"), prefer_local=True,
            prefer_free=True, min_context_window=8192),
        memory_policy=MemoryPolicy(
            max_entries=200, max_value_bytes=2000,
            allow_project_memory=False),
        verification_requirements=VerificationRequirements(
            require_tests=True, require_security=True,
            require_review=True, min_benchmark_pass_rate=1.0),
        resource_limits=ResourceLimits(
            max_runs_per_hour=60, max_concurrent=2,
            max_seconds_per_run=600.0, max_output_chars=20000,
            max_files_per_run=50),
    )


def research_template(name: str, purpose: str = "") -> AgentSpec:
    return AgentSpec(
        name=name,
        purpose=purpose or (
            "Investigate repositories and questions with real evidence; "
            "read-only, never modifies the project."),
        capabilities=("research", "reasoning"),
        tools=("read_file", "search", "git_status", "memory_read",
               "memory_write"),
        permissions=("read_file", "search_files", "git_status",
                     "git_diff"),
        model_requirements=ModelRequirements(
            ("research", "reasoning"), prefer_local=True,
            prefer_free=True, min_context_window=8192),
        memory_policy=MemoryPolicy(
            max_entries=200, max_value_bytes=2000,
            allow_project_memory=False),
        verification_requirements=VerificationRequirements(
            require_tests=False, require_security=True,
            require_review=True, min_benchmark_pass_rate=1.0),
        resource_limits=ResourceLimits(
            max_runs_per_hour=60, max_concurrent=2,
            max_seconds_per_run=300.0, max_output_chars=20000,
            max_files_per_run=10),
    )


def security_template(name: str, purpose: str = "") -> AgentSpec:
    return AgentSpec(
        name=name,
        purpose=purpose or (
            "Audit code for secrets, dangerous patterns, and risky "
            "dependencies; read-only, reports findings honestly."),
        capabilities=("security", "review"),
        tools=("read_file", "search", "git_status", "git_diff",
               "run_tests"),
        permissions=("read_file", "search_files", "git_status",
                     "git_diff", "run_tests"),
        model_requirements=ModelRequirements(
            ("security", "review"), prefer_local=True,
            prefer_free=True, min_context_window=8192),
        memory_policy=MemoryPolicy(
            max_entries=200, max_value_bytes=2000,
            allow_project_memory=False),
        verification_requirements=VerificationRequirements(
            require_tests=True, require_security=True,
            require_review=True, min_benchmark_pass_rate=1.0),
        resource_limits=ResourceLimits(
            max_runs_per_hour=60, max_concurrent=2,
            max_seconds_per_run=300.0, max_output_chars=20000,
            max_files_per_run=10),
    )


def game_development_template(name: str, purpose: str = "") -> AgentSpec:
    return AgentSpec(
        name=name,
        purpose=purpose or (
            "Build and iterate on game code, scenes, and assets "
            "through gated change sets with playable verification."),
        capabilities=("coding", "planning", "testing", "documentation"),
        tools=("read_file", "write_file", "run_tests", "search",
               "git_status", "git_diff", "terminal",
               "compute_execute"),
        permissions=("read_file", "search_files", "write_file",
                     "run_tests", "run_command", "git_status",
                     "git_diff"),
        model_requirements=ModelRequirements(
            ("coding", "planning"), prefer_local=True,
            prefer_free=True, min_context_window=8192),
        memory_policy=MemoryPolicy(
            max_entries=200, max_value_bytes=2000,
            allow_project_memory=False),
        verification_requirements=VerificationRequirements(
            require_tests=True, require_security=True,
            require_review=True, min_benchmark_pass_rate=1.0),
        resource_limits=ResourceLimits(
            max_runs_per_hour=30, max_concurrent=1,
            max_seconds_per_run=900.0, max_output_chars=20000,
            max_files_per_run=50),
    )


def os_development_template(name: str, purpose: str = "") -> AgentSpec:
    return AgentSpec(
        name=name,
        purpose=purpose or (
            "Develop operating-system components with extra care: "
            "bounded change sets, mandatory tests and security gates."),
        capabilities=("coding", "debugging", "testing", "security"),
        tools=("read_file", "write_file", "run_tests", "search",
               "git_status", "git_diff", "terminal",
               "compute_execute"),
        permissions=("read_file", "search_files", "write_file",
                     "run_tests", "run_command", "git_status",
                     "git_diff"),
        model_requirements=ModelRequirements(
            ("coding", "debugging", "security"), prefer_local=True,
            prefer_free=True, min_context_window=16384),
        memory_policy=MemoryPolicy(
            max_entries=200, max_value_bytes=2000,
            allow_project_memory=False),
        verification_requirements=VerificationRequirements(
            require_tests=True, require_security=True,
            require_review=True, min_benchmark_pass_rate=1.0),
        resource_limits=ResourceLimits(
            max_runs_per_hour=30, max_concurrent=1,
            max_seconds_per_run=900.0, max_output_chars=20000,
            max_files_per_run=30),
    )


def documentation_template(name: str, purpose: str = "") -> AgentSpec:
    return AgentSpec(
        name=name,
        purpose=purpose or (
            "Write and maintain accurate project documentation from "
            "real repository content; never fabricates APIs."),
        capabilities=("documentation", "research"),
        tools=("read_file", "write_file", "search", "git_status",
               "git_diff"),
        permissions=("read_file", "search_files", "write_file",
                     "git_status", "git_diff"),
        model_requirements=ModelRequirements(
            ("documentation", "research"), prefer_local=True,
            prefer_free=True, min_context_window=8192),
        memory_policy=MemoryPolicy(
            max_entries=200, max_value_bytes=2000,
            allow_project_memory=False),
        verification_requirements=VerificationRequirements(
            require_tests=False, require_security=True,
            require_review=True, min_benchmark_pass_rate=1.0),
        resource_limits=ResourceLimits(
            max_runs_per_hour=60, max_concurrent=2,
            max_seconds_per_run=300.0, max_output_chars=20000,
            max_files_per_run=30),
    )


TEMPLATES: dict[str, Callable[[str, str], AgentSpec]] = {
    "coding": coding_template,
    "research": research_template,
    "security": security_template,
    "game-development": game_development_template,
    "os-development": os_development_template,
    "documentation": documentation_template,
}


def template_names() -> list[str]:
    return list(TEMPLATE_NAMES)


def build_from_template(template: str, name: str,
                        purpose: str = "") -> AgentSpec:
    """Build a validated spec from one of the six templates."""
    key = (template or "").strip().lower()
    builder = TEMPLATES.get(key)
    if builder is None:
        raise ValueError(
            "Unknown template %r; expected one of: %s"
            % (template, ", ".join(TEMPLATE_NAMES)))
    return builder(name, purpose)


def describe_template(template: str) -> dict[str, Any]:
    """Return the template's default spec as a dict (placeholder name)."""
    key = (template or "").strip().lower()
    builder = TEMPLATES.get(key)
    if builder is None:
        raise ValueError(
            "Unknown template %r; expected one of: %s"
            % (template, ", ".join(TEMPLATE_NAMES)))
    return builder("template-preview").to_dict()


def describe_all() -> list[dict[str, Any]]:
    """Describe every template (name + default spec)."""
    return [{"template": name, "spec": describe_template(name)}
            for name in TEMPLATE_NAMES]
