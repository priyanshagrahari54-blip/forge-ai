"""Package validation (A81): structural and security checks.

Validation is deterministic and read-only. It answers one question:
"is this package internally consistent and inside its declared
envelope?" It never grants anything and never repairs a package — an
invalid package simply cannot leave the ``created`` state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forge.agent_engine.factory import TOOL_OPERATIONS, WRITE_OPERATIONS
from forge.agent_engine.spec import FORBIDDEN_TOOLS

SEVERITIES = ("info", "low", "medium", "high", "critical")
BLOCKING = frozenset({"high", "critical"})


@dataclass
class Finding:
    code: str
    severity: str
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity,
                "message": self.message}


@dataclass
class ValidationResult:
    valid: bool
    findings: list = field(default_factory=list)

    @property
    def blocking(self) -> list:
        return [f for f in self.findings if f.severity in BLOCKING]

    def to_dict(self) -> dict:
        return {"valid": self.valid,
                "findings": [f.to_dict() for f in self.findings],
                "blocking": [f.to_dict() for f in self.blocking]}


class PackageValidator:
    """Validate a built package against its own specification."""

    def validate(self, package: Any) -> ValidationResult:
        findings: list = []
        spec = package.spec
        runtime = package.runtime or {}
        gate = runtime.get("policy_gate", {})
        tools = runtime.get("tool_runtime", {})

        def add(code, severity, message):
            findings.append(Finding(code, severity, message))

        # -- self-grant / escalation ------------------------------------
        if gate.get("self_grant", False):
            add("self_grant", "critical",
                "The package claims permission self-grant, which is never "
                "permitted.")
        for tool in spec.tools:
            if tool in FORBIDDEN_TOOLS:
                add("forbidden_tool", "critical",
                    "Escalation tool declared: {0!r}".format(tool))

        # -- envelope consistency ---------------------------------------
        if set(gate.get("read_paths", ())) - set(spec.permissions.read_paths):
            add("scope_widened", "critical",
                "Runtime read scopes exceed the specification.")
        if set(gate.get("write_paths", ())) \
                - set(spec.permissions.write_paths):
            add("scope_widened", "critical",
                "Runtime write scopes exceed the specification.")
        if gate.get("allow_network") and not spec.permissions.allow_network:
            add("network_widened", "critical",
                "Runtime grants network access the spec does not.")
        if gate.get("allow_terminal") and not spec.permissions.allow_terminal:
            add("terminal_widened", "critical",
                "Runtime grants terminal access the spec does not.")
        if gate.get("allow_git_commit") \
                and not spec.permissions.allow_git_commit:
            add("commit_widened", "critical",
                "Runtime grants commit access the spec does not.")

        # -- tool grants -------------------------------------------------
        for grant in tools.get("grants", ()):
            tool = grant.get("tool", "")
            operation = grant.get("operation", "")
            if TOOL_OPERATIONS.get(tool) != operation:
                add("tool_mismatch", "critical",
                    "Tool {0!r} is wired to unexpected operation "
                    "{1!r}".format(tool, operation))
            if operation in WRITE_OPERATIONS:
                if not spec.permissions.write_paths:
                    add("write_without_scope", "critical",
                        "Write tool {0!r} has no declared scope".format(
                            tool))
                if spec.permissions.require_approval_for_writes \
                        and not grant.get("requires_approval", False):
                    add("approval_dropped", "critical",
                        "Write tool {0!r} skips the required "
                        "approval".format(tool))
        for tool in tools.get("unknown_tools", ()):
            add("unknown_tool", "medium",
                "Tool {0!r} has no runtime binding; it will never "
                "execute.".format(tool))

        # -- memory ------------------------------------------------------
        memory = runtime.get("memory", {})
        if spec.memory_policy.scope == "none" and memory.get("enabled"):
            add("memory_mismatch", "high",
                "Memory is enabled although the policy scope is 'none'.")
        if memory.get("max_bytes", 0) > spec.memory_policy.max_bytes:
            add("memory_widened", "high",
                "Runtime memory budget exceeds the policy.")

        # -- verification ------------------------------------------------
        verification = runtime.get("verification", {})
        if "security" not in verification.get("gates", ()):
            add("security_gate_missing", "critical",
                "The security gate is mandatory for every agent.")
        if set(spec.verification.required_gates) \
                - set(verification.get("gates", ())):
            add("gate_dropped", "critical",
                "The runtime drops verification gates the spec requires.")

        # -- checkpoints -------------------------------------------------
        checkpoints = runtime.get("checkpoints", {})
        if spec.permissions.write_paths and not checkpoints.get("enabled") \
                and spec.verification.require_checkpoint:
            add("checkpoint_missing", "high",
                "A writing agent must checkpoint before its first write.")

        # -- fabric ------------------------------------------------------
        fabric = runtime.get("model_fabric", {})
        if fabric.get("capability") != spec.model_requirements.capability:
            add("fabric_mismatch", "high",
                "The routing capability does not match the spec.")

        # -- advisory ----------------------------------------------------
        if not spec.tools:
            add("no_tools", "info",
                "This agent declares no tools; it can only reason.")
        if spec.permissions.write_paths \
                and not spec.permissions.require_approval_for_writes:
            add("unapproved_writes", "medium",
                "This agent writes without per-write approval; ensure the "
                "operating mode is intentional.")

        blocking = [f for f in findings if f.severity in BLOCKING]
        return ValidationResult(valid=not blocking, findings=findings)
