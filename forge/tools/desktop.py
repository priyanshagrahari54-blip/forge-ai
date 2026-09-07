"""Desktop permission foundation with a safe mock (A33).

This module defines the permission abstractions for a future Desktop Agent
(:class:`DesktopResource`, :class:`DesktopAction`, :class:`DesktopPermission`,
:class:`DesktopActionRequest`) plus a policy-gated :class:`MockDesktop`.

Only the permission and policy abstraction exists here. There is no screen
capture, no mouse or keyboard control, no application launching, and no
window interaction — the mock records intended actions and returns canned
observations. Unrestricted computer control is explicitly out of scope.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from forge.security.approvals import ApprovalRequest, ApprovalStore, enforce_with_token
from forge.security.audit import AuditLog
from forge.security.policy import PermissionPolicy, PermissionRequest, Resource
from forge.security.policy_gate import PolicyDecision


class DesktopAction(str, Enum):
    READ_SCREEN = "read_screen"
    MOUSE_MOVE = "mouse_move"
    MOUSE_CLICK = "mouse_click"
    KEYBOARD = "keyboard"
    LAUNCH = "launch"
    WINDOW = "window"
    CLIPBOARD = "clipboard"
    FILE_ACCESS = "file_access"


@dataclass(frozen=True)
class DesktopResource:
    """A desktop entity an action would target (never touched directly)."""

    kind: str  # application | screen | window | file | clipboard
    target: str = ""

    def scope(self) -> str:
        return f"{self.kind}:{self.target}" if self.target else self.kind


@dataclass(frozen=True)
class DesktopActionRequest:
    """A proposed desktop action: WHAT on WHERE, by WHOM, WHY."""

    action: DesktopAction | str
    resource: DesktopResource
    agent: str
    task_id: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", DesktopAction(self.action))

    def to_permission_request(self) -> PermissionRequest:
        return PermissionRequest(
            agent=self.agent, resource=Resource.DESKTOP,
            operation=self.action.value, scope=self.resource.scope(),
            task_id=self.task_id, reason=self.reason or "desktop action")


@dataclass(frozen=True)
class DesktopPermission:
    """Evaluated authorization for one desktop action request."""

    allowed: bool
    decision: PolicyDecision
    reason: str = ""
    approval_required: bool = False
    approval_request_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "decision": self.decision.value,
            "reason": self.reason,
            "approval_required": self.approval_required,
            "approval_request_id": self.approval_request_id,
        }


@dataclass
class DesktopResult:
    allowed: bool
    action: str
    target: str
    decision: str
    reason: str = ""
    observation: str = ""
    approval_required: bool = False
    approval_request_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "target": self.target,
            "decision": self.decision,
            "reason": self.reason,
            "observation_chars": len(self.observation),
            "approval_required": self.approval_required,
            "approval_request_id": self.approval_request_id,
        }


class MockDesktop:
    """Policy-gated mock desktop. Records intentions; controls nothing."""

    def __init__(self, policy: PermissionPolicy | None = None,
                 store: ApprovalStore | None = None,
                 audit: AuditLog | None = None) -> None:
        # No policy means no authorization: fail closed.
        self.policy = policy if policy is not None else PermissionPolicy()
        self.store = store
        self.audit = audit
        self.actions: list[dict[str, str]] = []

    def check(self, request: DesktopActionRequest) -> DesktopPermission:
        """Evaluate a desktop action without performing it."""
        permission = request.to_permission_request()
        evaluation = self.policy.evaluate(permission)
        if self.audit is not None:
            self.audit.record_evaluation(permission, evaluation)
        if evaluation.decision == PolicyDecision.ALLOW:
            return DesktopPermission(True, evaluation.decision,
                                     evaluation.reason)
        if evaluation.decision == PolicyDecision.REQUIRE_APPROVAL:
            return DesktopPermission(
                False, evaluation.decision, evaluation.reason,
                approval_required=True,
                approval_request_id=self._file_approval(request))
        return DesktopPermission(False, evaluation.decision,
                                 evaluation.reason)

    def perform(self, request: DesktopActionRequest,
                approval_token_id: str = "") -> DesktopResult:
        """Evaluate and, when authorized, record a mock desktop action."""
        permission = request.to_permission_request()
        evaluation = self.policy.evaluate(permission)
        if self.audit is not None:
            self.audit.record_evaluation(permission, evaluation)
        target = request.resource.scope()
        if evaluation.decision == PolicyDecision.ALLOW:
            return self._record(request, evaluation)
        if evaluation.decision == PolicyDecision.REQUIRE_APPROVAL:
            allowed, reason = enforce_with_token(
                self.store, approval_token_id, permission)
            if allowed:
                return self._record(request, evaluation)
            return DesktopResult(
                False, request.action.value, target,
                PolicyDecision.REQUIRE_APPROVAL.value, reason,
                approval_required=True,
                approval_request_id=self._file_approval(request))
        return DesktopResult(False, request.action.value, target,
                             PolicyDecision.DENY.value, evaluation.reason)

    def _record(self, request: DesktopActionRequest,
                evaluation: Any) -> DesktopResult:
        target = request.resource.scope()
        self.actions.append({"action": request.action.value, "target": target})
        observation = f"<mock {request.action.value} {target}>"
        return DesktopResult(True, request.action.value, target,
                             PolicyDecision.ALLOW.value, evaluation.reason,
                             observation=observation)

    def _file_approval(self, request: DesktopActionRequest) -> str:
        if self.store is None:
            return ""
        approval = ApprovalRequest(
            agent=request.agent, resource=Resource.DESKTOP,
            operation=request.action.value,
            scopes=(request.resource.scope(),), task_id=request.task_id,
            reason=request.reason or f"Desktop {request.action.value}",
            consequences="The agent will interact with the desktop.")
        return self.store.submit(approval).id
