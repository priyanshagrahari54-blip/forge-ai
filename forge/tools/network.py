"""Network permission foundation with a safe mock (A33).

:class:`MockNetwork` is the enforcement point for network policy: every
request is evaluated against the permission engine (host, port, protocol),
audited, and served from deterministic fixtures. No sockets are opened.
Unknown destinations fail closed; nothing here weakens TLS verification
because nothing here performs real network I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forge.security.approvals import ApprovalRequest, ApprovalStore, enforce_with_token
from forge.security.audit import AuditLog
from forge.security.policy import (
    PermissionEvaluation,
    PermissionPolicy,
    PermissionRequest,
    Resource,
)
from forge.security.policy_gate import PolicyDecision


@dataclass
class NetworkResult:
    allowed: bool
    host: str
    port: int
    protocol: str
    decision: str
    reason: str = ""
    response: str = ""
    approval_required: bool = False
    approval_request_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "host": self.host,
            "port": self.port,
            "protocol": self.protocol,
            "decision": self.decision,
            "reason": self.reason,
            "response_chars": len(self.response),
            "approval_required": self.approval_required,
            "approval_request_id": self.approval_request_id,
        }


class MockNetwork:
    """Policy-gated mock network serving deterministic fixtures."""

    def __init__(self, policy: PermissionPolicy | None = None,
                 store: ApprovalStore | None = None,
                 audit: AuditLog | None = None) -> None:
        # No policy means no authorization: fail closed.
        self.policy = policy if policy is not None else PermissionPolicy()
        self.store = store
        self.audit = audit
        self._served: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []

    def serve(self, host: str, response: str) -> None:
        """Register a deterministic fixture response for a host."""
        self._served[host.lower()] = response

    def check(self, host: str, port: int, protocol: str, *,
              agent: str, task_id: str = "") -> PermissionEvaluation:
        """Evaluate a network request without performing it.

        Future real implementations must call this (or the engine directly)
        before any I/O.
        """
        request = PermissionRequest(
            agent=agent, resource=Resource.NETWORK, operation="request",
            task_id=task_id, reason="network request",
            details=(("host", host), ("port", port),
                     ("protocol", protocol)))
        return self.policy.evaluate(request)

    def request(self, host: str, port: int, protocol: str, *,
                agent: str, task_id: str = "",
                approval_token_id: str = "") -> NetworkResult:
        """Perform a gated mock network request."""
        permission = PermissionRequest(
            agent=agent, resource=Resource.NETWORK, operation="request",
            task_id=task_id, reason="network request",
            details=(("host", host), ("port", port),
                     ("protocol", protocol)))
        evaluation = self.policy.evaluate(permission)
        if self.audit is not None:
            self.audit.record_evaluation(permission, evaluation)
        if evaluation.decision == PolicyDecision.ALLOW:
            return self._perform(host, port, protocol, evaluation)
        if evaluation.decision == PolicyDecision.REQUIRE_APPROVAL:
            allowed, reason = enforce_with_token(
                self.store, approval_token_id, permission)
            if allowed:
                return self._perform(host, port, protocol, evaluation)
            return NetworkResult(
                False, host, port, protocol,
                PolicyDecision.REQUIRE_APPROVAL.value, reason,
                approval_required=True,
                approval_request_id=self._file_approval(
                    host, port, protocol, agent, task_id))
        return NetworkResult(False, host, port, protocol,
                             PolicyDecision.DENY.value, evaluation.reason)

    def _perform(self, host: str, port: int, protocol: str,
                 evaluation: PermissionEvaluation) -> NetworkResult:
        self.calls.append({"host": host, "port": port, "protocol": protocol})
        response = self._served.get(host.lower(), f"<mock {host}>")
        return NetworkResult(True, host, port, protocol,
                             PolicyDecision.ALLOW.value, evaluation.reason,
                             response=response)

    def _file_approval(self, host: str, port: int, protocol: str,
                       agent: str, task_id: str) -> str:
        if self.store is None:
            return ""
        try:
            request = ApprovalRequest(
                agent=agent, resource=Resource.NETWORK, operation="request",
                scopes=(host,), task_id=task_id,
                reason=f"Network request to {host}:{port}/{protocol}",
                consequences="The agent will contact an external host.",
                bind=(("port", port), ("protocol", protocol)))
        except ValueError:
            return ""
        return self.store.submit(request).id
