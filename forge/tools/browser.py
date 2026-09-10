"""Browser permission foundation with a safe mock (A33).

:class:`MockBrowser` is the enforcement point for browser policy: every
action is evaluated against the permission engine, audited, and served from
deterministic fixtures. There is no real browser automation here — no
navigation, clicks, or uploads ever leave the process. Unauthorized domains
fail closed; mutating actions (submit/upload) need explicit authorization.

Credential handling, authentication bypass, and CAPTCHA/security-control
bypass are explicitly out of scope and must never be added.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from forge.security.approvals import ApprovalRequest, ApprovalStore, enforce_with_token
from forge.security.audit import AuditLog
from forge.security.policy import PermissionPolicy, PermissionRequest, Resource
from forge.security.policy_gate import PolicyDecision


class BrowserAction(str, Enum):
    NAVIGATE = "navigate"
    READ = "read"
    CLICK = "click"
    TYPE = "type"
    SUBMIT = "submit"
    UPLOAD = "upload"
    DOWNLOAD = "download"


@dataclass
class BrowserResult:
    allowed: bool
    action: str
    url: str
    decision: str
    reason: str = ""
    content: str = ""
    approval_required: bool = False
    approval_request_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "url": self.url,
            "decision": self.decision,
            "reason": self.reason,
            "content_chars": len(self.content),
            "approval_required": self.approval_required,
            "approval_request_id": self.approval_request_id,
        }


class MockBrowser:
    """Policy-gated mock browser serving deterministic fixtures."""

    def __init__(self, policy: PermissionPolicy | None = None,
                 store: ApprovalStore | None = None,
                 audit: AuditLog | None = None) -> None:
        # No policy means no authorization: fail closed.
        self.policy = policy if policy is not None else PermissionPolicy()
        self.store = store
        self.audit = audit
        self._served: dict[str, str] = {}
        self.visits: list[dict[str, str]] = []

    def serve(self, url: str, content: str) -> None:
        """Register deterministic fixture content for a URL."""
        self._served[url] = content

    def _evaluate(self, action: BrowserAction, url: str, agent: str,
                  task_id: str) -> tuple[PermissionRequest, Any]:
        request = PermissionRequest(
            agent=agent, resource=Resource.BROWSER,
            operation=action.value, scope=url, task_id=task_id,
            reason=f"browser {action.value}")
        return request, self.policy.evaluate(request)

    def _file_approval(self, action: BrowserAction, url: str, agent: str,
                       task_id: str) -> str:
        if self.store is None:
            return ""
        try:
            from urllib.parse import urlsplit
            host = urlsplit(url).hostname or url
            request = ApprovalRequest(
                agent=agent, resource=Resource.BROWSER, operation=action.value,
                scopes=(host,), task_id=task_id,
                reason=f"Browser {action.value} on {host}",
                consequences=f"The agent will {action.value} {url}.")
        except ValueError:
            return ""
        return self.store.submit(request).id

    def act(self, action: BrowserAction | str, url: str, *,
            agent: str, task_id: str = "",
            approval_token_id: str = "") -> BrowserResult:
        """Perform a gated browser action, serving fixtures when allowed."""
        action = BrowserAction(action)
        request, evaluation = self._evaluate(action, url, agent, task_id)
        if self.audit is not None:
            self.audit.record_evaluation(request, evaluation)
        if evaluation.decision == PolicyDecision.ALLOW:
            return self._perform(action, url, evaluation)
        if evaluation.decision == PolicyDecision.REQUIRE_APPROVAL:
            allowed, reason = enforce_with_token(
                self.store, approval_token_id, request)
            if allowed:
                return self._perform(action, url, evaluation)
            return BrowserResult(
                False, action.value, url,
                PolicyDecision.REQUIRE_APPROVAL.value, reason,
                approval_required=True,
                approval_request_id=self._file_approval(
                    action, url, agent, task_id))
        return BrowserResult(False, action.value, url,
                             PolicyDecision.DENY.value, evaluation.reason)

    def _perform(self, action: BrowserAction, url: str,
                 evaluation: Any) -> BrowserResult:
        self.visits.append({"action": action.value, "url": url})
        content = self._served.get(url, f"<mock {action.value} {url}>") \
            if action in (BrowserAction.NAVIGATE, BrowserAction.READ) else ""
        return BrowserResult(True, action.value, url,
                             PolicyDecision.ALLOW.value, evaluation.reason,
                             content=content)

    # -- convenience ------------------------------------------------------------

    def navigate(self, url: str, **kwargs: Any) -> BrowserResult:
        return self.act(BrowserAction.NAVIGATE, url, **kwargs)

    def read(self, url: str, **kwargs: Any) -> BrowserResult:
        return self.act(BrowserAction.READ, url, **kwargs)

    def submit(self, url: str, **kwargs: Any) -> BrowserResult:
        return self.act(BrowserAction.SUBMIT, url, **kwargs)

    def upload(self, url: str, **kwargs: Any) -> BrowserResult:
        return self.act(BrowserAction.UPLOAD, url, **kwargs)
