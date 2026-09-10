"""Voice command foundation (A33).

Voice is just another request source: a :class:`VoiceCommand` is parsed into
a :class:`VoiceIntent`, evaluated against the normal permission system, and
approved when policy requires it. Voice can never bypass permissions.

Only deterministic template matching exists here. Real speech recognition,
wake-word detection, and audio handling are explicitly out of scope.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from forge.security.approvals import ApprovalRequest, ApprovalStore, enforce_with_token
from forge.security.audit import AuditLog
from forge.security.policy import PermissionPolicy, PermissionRequest, Resource
from forge.security.policy_gate import PolicyDecision


@dataclass(frozen=True)
class VoiceCommand:
    """A transcribed voice command (transcription itself is out of scope)."""

    text: str
    agent: str = "VoiceInterface"
    task_id: str = ""


@dataclass(frozen=True)
class VoiceIntent:
    """Parsed intent: WHAT the speaker asked for, with extracted slots."""

    name: str
    slots: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    raw: str = ""

    @property
    def known(self) -> bool:
        return self.name != "unknown" and self.confidence > 0.0


@dataclass(frozen=True)
class VoicePermission:
    """Evaluated authorization for one voice intent."""

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
class VoiceCommandResult:
    ok: bool
    intent: VoiceIntent
    message: str = ""
    task: dict[str, Any] | None = None
    permission: VoicePermission | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "intent": self.intent.name,
            "slots": dict(self.intent.slots),
            "message": self.message,
            "task": self.task,
            "permission": self.permission.to_dict() if self.permission else None,
        }


#: Deterministic command templates: (pattern, intent, slot names).
_TEMPLATES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (r"update the website", "update_website", ()),
    (r"summarize (.+)", "summarize", ("target",)),
    (r"run (?:the )?tests", "run_tests", ()),
    (r"commit(?: the)? changes", "commit", ()),
    (r"review (.+)", "review", ("target",)),
    (r"check status", "status", ()),
)


class VoiceInterface:
    """Parse voice commands and route them through permission evaluation."""

    def __init__(self, policy: PermissionPolicy | None = None,
                 store: ApprovalStore | None = None,
                 audit: AuditLog | None = None) -> None:
        # No policy means no authorization: fail closed.
        self.policy = policy if policy is not None else PermissionPolicy()
        self.store = store
        self.audit = audit

    def parse(self, command: VoiceCommand) -> VoiceIntent:
        """Match a command against the deterministic templates."""
        text = re.sub(r"^forge[,\s]+", "", command.text.strip().lower())
        text = re.sub(r"[.?!]+$", "", text).strip()
        for pattern, name, slots in _TEMPLATES:
            match = re.fullmatch(pattern, text)
            if match:
                return VoiceIntent(
                    name=name,
                    slots=dict(zip(slots, match.groups())),
                    confidence=1.0, raw=command.text)
        return VoiceIntent(name="unknown", confidence=0.0, raw=command.text)

    def check(self, command: VoiceCommand) -> tuple[VoiceIntent, VoicePermission]:
        """Parse and evaluate a command without acting on it."""
        intent = self.parse(command)
        if not intent.known:
            return intent, VoicePermission(
                False, PolicyDecision.DENY, "Unknown voice intent")
        permission = PermissionRequest(
            agent=command.agent, resource=Resource.VOICE, operation="command",
            scope=intent.name, task_id=command.task_id,
            reason=f"voice intent {intent.name}")
        evaluation = self.policy.evaluate(permission)
        if self.audit is not None:
            self.audit.record_evaluation(permission, evaluation)
        if evaluation.decision == PolicyDecision.ALLOW:
            return intent, VoicePermission(True, evaluation.decision,
                                           evaluation.reason)
        if evaluation.decision == PolicyDecision.REQUIRE_APPROVAL:
            return intent, VoicePermission(
                False, evaluation.decision, evaluation.reason,
                approval_required=True,
                approval_request_id=self._file_approval(command, intent))
        return intent, VoicePermission(False, evaluation.decision,
                                       evaluation.reason)

    def handle(self, command: VoiceCommand, *,
               task_factory: Callable[[VoiceIntent], dict[str, Any]] | None = None,
               approval_token_id: str = "") -> VoiceCommandResult:
        """Parse, authorize, and (when allowed) turn a command into a task."""
        intent, permission = self.check(command)
        if not intent.known:
            return VoiceCommandResult(False, intent,
                                      "Unknown voice command; no action taken.",
                                      permission=permission)
        if permission.decision == PolicyDecision.REQUIRE_APPROVAL and approval_token_id:
            probe = PermissionRequest(
                agent=command.agent, resource=Resource.VOICE,
                operation="command", scope=intent.name,
                task_id=command.task_id)
            allowed, reason = enforce_with_token(
                self.store, approval_token_id, probe)
            if allowed:
                permission = VoicePermission(True, PolicyDecision.ALLOW, reason)
        if not permission.allowed:
            return VoiceCommandResult(
                False, intent,
                permission.reason or "Voice command not permitted.",
                permission=permission)
        factory = task_factory or (lambda item: {
            "id": f"voice-{abs(hash(item.name)) % 10_000:04d}",
            "description": item.raw or item.name,
            "intent": item.name, "slots": dict(item.slots)})
        return VoiceCommandResult(True, intent, "Task created.",
                                  task=factory(intent), permission=permission)

    def _file_approval(self, command: VoiceCommand,
                       intent: VoiceIntent) -> str:
        if self.store is None:
            return ""
        request = ApprovalRequest(
            agent=command.agent, resource=Resource.VOICE, operation="command",
            scopes=(intent.name,), task_id=command.task_id,
            reason=f"Voice command: {command.text!r}",
            consequences="The voice command will create a task.")
        return self.store.submit(request).id
