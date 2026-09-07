"""Explicit permission/policy gate (A32.2).

Every proposed change set passes through :class:`PolicyGate` *before* any
modification. Each evaluation sees the full request context — operation,
path, tool, risk, and requested capability — and returns one explicit
decision:

- ``ALLOW`` — the action may proceed.
- ``DENY`` — the action is forbidden; approval cannot override it.
- ``REQUIRE_APPROVAL`` — the action may proceed only with explicit approval.

Modes mirror :class:`forge.security.permissions.OperationMode`:

- ``safe`` / ``locked`` — read/search/analyze only; every modification is
  denied, even with approval.
- ``assisted`` (default, approval-required) — modifications require explicit
  approval.
- ``autonomous`` — low-risk project-scope writes are auto-approved; sensitive
  operations and high-risk writes still require explicit approval.

A denial is never silently bypassed: ``DENY`` ignores the approval flag, and
callers record the decision (see ``ChangeApplier``) instead of proceeding.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

from forge.security.permissions import (
    AUTONOMOUS_AUTO_OPERATIONS,
    READ_OPERATIONS,
    OperationMode,
    PermissionLevel,
    PermissionManager,
)

#: Operations that mutate or remove repository content.
WRITE_OPERATIONS = frozenset({"write_file", "delete_file"})

#: Risk levels callers may attach to a change. Anything unrecognized is
#: treated as high risk (fail closed).
KNOWN_RISKS = frozenset({"NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"})
HIGH_RISKS = frozenset({"HIGH", "CRITICAL"})

_CREDENTIAL_FILENAME_TOKENS = ("credential", "secret", "private_key", "token")


class PolicyDecision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


@dataclass(frozen=True)
class PolicyOutcome:
    decision: PolicyDecision
    allowed: bool
    reason: str
    operation: str = ""
    path: str = ""
    tool: str = ""
    risk: str = "NONE"
    capability: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "allowed": self.allowed,
            "reason": self.reason,
            "operation": self.operation,
            "path": self.path,
            "tool": self.tool,
            "risk": self.risk,
            "capability": self.capability,
        }


def _normalize_risk(risk: str) -> str:
    candidate = (risk or "NONE").upper()
    if candidate in KNOWN_RISKS:
        return candidate
    return "HIGH"  # fail closed on unrecognized risk labels


def _is_protected_path(path: str) -> bool:
    """True for paths policy must never allow writes/deletes to."""
    if not path:
        return False
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts or "\\" in path:
        return True
    if ".git" in candidate.parts or ".forge" in candidate.parts:
        return True
    name = candidate.name.lower()
    if name == ".env" or name.endswith(".env"):
        return True
    return any(token in name for token in _CREDENTIAL_FILENAME_TOKENS)


class PolicyGate:
    """Evaluate write/delete requests against mode + operation + risk policy."""

    def __init__(self, manager: PermissionManager | Any | None = None) -> None:
        self.manager = manager if manager is not None else PermissionManager()

    @property
    def mode(self) -> OperationMode:
        return getattr(self.manager, "mode", OperationMode.ASSISTED)

    def _level(self, operation: str) -> PermissionLevel:
        check = getattr(self.manager, "check", None)
        if callable(check):
            return check(operation)
        return PermissionLevel.APPROVAL_REQUIRED

    def evaluate(self, *, operation: str, path: str = "", tool: str = "",
                 risk: str = "NONE", capability: str = "",
                 approved: bool = False,
                 mode: OperationMode | str | None = None) -> PolicyOutcome:
        """Return the explicit policy decision for one requested action."""
        active = OperationMode(mode) if mode is not None else self.mode
        normalized_risk = _normalize_risk(risk)
        context = {
            "operation": operation, "path": path, "tool": tool,
            "risk": normalized_risk, "capability": capability,
        }

        if operation in WRITE_OPERATIONS and _is_protected_path(path):
            return PolicyOutcome(
                PolicyDecision.DENY, False,
                f"Write to protected path denied: {path!r}", **context)

        if active in (OperationMode.SAFE, OperationMode.LOCKED):
            if operation in READ_OPERATIONS:
                return PolicyOutcome(
                    PolicyDecision.ALLOW, True,
                    f"Read-only operation allowed in {active.value} mode", **context)
            return PolicyOutcome(
                PolicyDecision.DENY, False,
                f"Operation denied in {active.value} mode", **context)

        if self._level(operation) == PermissionLevel.BLOCKED:
            return PolicyOutcome(
                PolicyDecision.DENY, False,
                "Operation blocked by security policy.", **context)

        if self._level(operation) == PermissionLevel.SAFE:
            return PolicyOutcome(
                PolicyDecision.ALLOW, True, "", **context)

        # Approval-required operation from here on.
        if (active == OperationMode.AUTONOMOUS
                and operation in AUTONOMOUS_AUTO_OPERATIONS
                and normalized_risk not in HIGH_RISKS):
            return PolicyOutcome(
                PolicyDecision.ALLOW, True,
                "Auto-approved low-risk write in autonomous mode", **context)
        if approved:
            return PolicyOutcome(
                PolicyDecision.ALLOW, True, "Explicit approval granted", **context)
        if normalized_risk in HIGH_RISKS and active == OperationMode.AUTONOMOUS:
            return PolicyOutcome(
                PolicyDecision.REQUIRE_APPROVAL, False,
                "High-risk writes require explicit approval even in "
                "autonomous mode", **context)
        return PolicyOutcome(
            PolicyDecision.REQUIRE_APPROVAL, False,
            "Approval required before executing this operation.", **context)
