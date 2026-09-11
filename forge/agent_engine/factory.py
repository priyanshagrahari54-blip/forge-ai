"""Agent factory (A81): turn a specification into a structured package.

The factory is deterministic and side-effect free: the same
specification always produces the same runtime plan, the same system
prompt, and the same package id. It writes nothing, executes nothing,
and grants nothing — building a package only *describes* an agent.

Two hard rules are enforced here and re-checked by the validator:

1. A built package can never request a permission the specification did
   not declare (the runtime plan is derived from the spec only).
2. A built package can never request escalation tools; those are
   rejected at spec parse time and re-rejected here as defence in
   depth.
"""
from __future__ import annotations

from typing import Any, Mapping

from forge.agent_engine.lifecycle import CREATED
from forge.agent_engine.package import AgentPackage
from forge.agent_engine.spec import FORBIDDEN_TOOLS, AgentSpec, SpecError
from forge.agent_engine.templates import from_template
from forge.agent_engine.version import AgentVersion

#: Tools the factory knows how to wire, mapped to the runtime operation
#: the permission platform will be asked to authorize.
TOOL_OPERATIONS = {
    "read_file": "read_file",
    "write_file": "write_file",
    "delete_file": "delete_file",
    "search": "search_code",
    "run_tests": "run_tests",
    "build": "run_tests",
    "web_fetch": "network_request",
    "terminal": "run_command",
    "git_commit": "git_commit",
    "git_status": "git_status",
}

#: Operations that mutate the repository.
WRITE_OPERATIONS = frozenset({"write_file", "delete_file"})


def _prompt_for(spec: AgentSpec) -> str:
    lines = [
        "You are {0}, a specialized Forge agent.".format(spec.name),
        "",
        "Purpose: {0}".format(spec.purpose),
        "",
        "Capabilities: {0}".format(", ".join(spec.capabilities)),
        "Tools you may request: {0}".format(
            ", ".join(spec.tools) if spec.tools else "none"),
        "",
        "Operating rules:",
        "- Every action is authorized by the Forge permission platform "
        "before it happens. You cannot grant yourself permissions, "
        "widen your scopes, or bypass an approval.",
        "- You may only read: {0}".format(
            ", ".join(spec.permissions.read_paths) or "nothing"),
        "- You may only propose writes under: {0}".format(
            ", ".join(spec.permissions.write_paths) or "nothing"),
        "- Network access: {0}".format(
            "allowed for " + ", ".join(spec.permissions.domains)
            if spec.permissions.allow_network and spec.permissions.domains
            else ("allowed" if spec.permissions.allow_network
                  else "denied")),
        "- Terminal access: {0}".format(
            "allowed" if spec.permissions.allow_terminal else "denied"),
        "- Your work is accepted only after these gates pass: {0}".format(
            ", ".join(spec.verification.required_gates)),
        "- Never store secrets, credentials, or tokens in memory or "
        "output.",
        "- If you cannot complete the task within your limits, say so "
        "honestly instead of guessing.",
    ]
    return "\n".join(lines)


class AgentFactoryEngine:
    """Builds structured agent packages from validated specifications."""

    def build(self, spec: Any, *, version: Any = "1.0.0",
              built_by: str = "") -> AgentPackage:
        if isinstance(spec, Mapping):
            spec = AgentSpec.from_dict(spec)
        if not isinstance(spec, AgentSpec):
            raise SpecError("build() needs an AgentSpec or a mapping")

        for tool in spec.tools:
            if tool in FORBIDDEN_TOOLS:
                raise SpecError(
                    "Refusing to package escalation tool {0!r}".format(
                        tool))

        tool_grants = []
        unknown_tools = []
        for tool in spec.tools:
            operation = TOOL_OPERATIONS.get(tool, "")
            if not operation:
                unknown_tools.append(tool)
                continue
            writes = operation in WRITE_OPERATIONS
            if writes and not spec.permissions.write_paths:
                raise SpecError(
                    "Tool {0!r} writes but the specification declares no "
                    "write scopes".format(tool))
            if operation == "network_request" \
                    and not spec.permissions.allow_network:
                raise SpecError(
                    "Tool {0!r} needs network access, which is not "
                    "granted".format(tool))
            if operation == "run_command" \
                    and not spec.permissions.allow_terminal:
                raise SpecError(
                    "Tool {0!r} needs terminal access, which is not "
                    "granted".format(tool))
            if operation == "git_commit" \
                    and not spec.permissions.allow_git_commit:
                raise SpecError(
                    "Tool {0!r} needs commit access, which is not "
                    "granted".format(tool))
            tool_grants.append({
                "tool": tool,
                "operation": operation,
                "writes": writes,
                "scopes": list(spec.permissions.write_paths if writes
                               else spec.permissions.read_paths),
                "requires_approval": bool(
                    writes and spec.permissions.require_approval_for_writes),
            })

        memory = spec.memory_policy
        namespace = memory.namespace or "agents/{0}".format(spec.name)

        runtime = {
            "model_fabric": {
                "capability": spec.model_requirements.capability,
                "required_capabilities": list(spec.capabilities),
                "min_context_window":
                    spec.model_requirements.min_context_window,
                "max_output_tokens":
                    spec.model_requirements.max_output_tokens,
                "prefer_local": spec.model_requirements.prefer_local,
                "prefer_free": spec.model_requirements.prefer_free,
                "complexity": spec.model_requirements.complexity,
                "fallback_capability":
                    spec.model_requirements.fallback_capability,
            },
            "policy_gate": {
                "read_paths": list(spec.permissions.read_paths),
                "write_paths": list(spec.permissions.write_paths),
                "allow_network": spec.permissions.allow_network,
                "allow_terminal": spec.permissions.allow_terminal,
                "allow_git_commit": spec.permissions.allow_git_commit,
                "domains": list(spec.permissions.domains),
                "require_approval_for_writes":
                    spec.permissions.require_approval_for_writes,
                "self_grant": False,
            },
            "tool_runtime": {
                "grants": tool_grants,
                "unknown_tools": unknown_tools,
            },
            "memory": {
                "scope": memory.scope,
                "namespace": namespace,
                "max_entries": memory.max_entries,
                "max_bytes": memory.max_bytes,
                "ttl_seconds": memory.ttl_seconds,
                "enabled": memory.scope != "none",
            },
            "verification": {
                "gates": list(spec.verification.required_gates),
                "independent_review":
                    spec.verification.require_independent_review,
                "max_repair_attempts":
                    spec.verification.max_repair_attempts,
            },
            "checkpoints": {
                "enabled": spec.verification.require_checkpoint,
                "before_first_write": True,
                "restore_on_failure": True,
            },
            "resource_limits": spec.resource_limits.to_dict(),
        }

        return AgentPackage(
            spec=spec,
            version=AgentVersion.parse(version),
            runtime=runtime,
            prompt=_prompt_for(spec),
            state=CREATED,
            built_by=str(built_by or "")[:64],
        )

    def build_from_template(self, template: str, *,
                            overrides: Mapping = None,
                            version: Any = "1.0.0",
                            built_by: str = "") -> AgentPackage:
        spec = from_template(template, overrides)
        return self.build(spec, version=version, built_by=built_by)
