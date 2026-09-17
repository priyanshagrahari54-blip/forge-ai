"""Permission audit log (A33).

Every permission decision is observable through structured
:class:`PermissionAuditEvent` values: timestamp, request/task/trace ids,
agent, resource, operation, scope, risk, decision, matched rules, and the
approval linkage. Secret-looking values are redacted with the same
implementation as task reports (``forge.core.report.redact``), so API keys,
passwords, tokens, private keys, and raw secrets can never land in the log.

The in-memory log is authoritative. The optional JSONL sink is best-effort
for ordinary telemetry-like audit consumers, but security-sensitive writes
can request strict sink durability: a sink failure then raises instead of
silently converting a missing security record into an apparent success.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from forge.core.report import redact
from forge.security.policy_gate import PolicyDecision


class AuditSinkError(IOError):
    """Raised when a security-sensitive audit event cannot reach its sink."""


@dataclass(frozen=True)
class PermissionAuditEvent:
    """One recorded permission decision. Free text is redacted on creation."""

    agent: str
    resource: str
    operation: str
    decision: PolicyDecision | str
    scope: str = ""
    risk: str = "NONE"
    reason: str = ""
    task_id: str = ""
    trace_id: str = ""
    request_id: str = field(default_factory=lambda: uuid4().hex)
    matched_rule: tuple[str, ...] = ()
    policy_rule: str = ""
    approval_required: bool = False
    approval_id: str = ""
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        decision = (self.decision if isinstance(self.decision, PolicyDecision)
                    else PolicyDecision(str(self.decision).upper()))
        object.__setattr__(self, "decision", decision)
        for field_name in ("agent", "resource", "operation", "scope", "risk",
                           "reason", "task_id", "trace_id", "request_id",
                           "policy_rule", "approval_id"):
            value = getattr(self, field_name)
            if isinstance(value, str):
                object.__setattr__(self, field_name, redact(value))
        object.__setattr__(self, "matched_rule",
                           tuple(redact(rule) for rule in self.matched_rule))

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "request_id": self.request_id,
            "task_id": self.task_id,
            "trace_id": self.trace_id,
            "agent": self.agent,
            "resource": self.resource,
            "operation": self.operation,
            "scope": self.scope,
            "risk": self.risk,
            "decision": self.decision.value,
            "matched_rule": list(self.matched_rule),
            "policy_rule": self.policy_rule,
            "approval_required": self.approval_required,
            "approval_id": self.approval_id,
            "reason": self.reason,
        }


class AuditLog:
    """Append-only, queryable record of permission decisions."""

    def __init__(self, sink_path: str | Path | None = None) -> None:
        self._events: list[PermissionAuditEvent] = []
        self._sink = Path(sink_path) if sink_path is not None else None
        self.sink_errors = 0

    def __len__(self) -> int:
        return len(self._events)

    @property
    def events(self) -> tuple[PermissionAuditEvent, ...]:
        return tuple(self._events)

    def record(self, event: PermissionAuditEvent, *,
               security_sensitive: bool = False) -> PermissionAuditEvent:
        """Record an event; strict security writes fail closed on sink errors.

        The in-memory append happens first so the event remains observable for
        diagnostics. A security-sensitive sink failure is then surfaced to the
        caller, which must treat the protected operation as failed/denied.
        Ordinary records retain the historical best-effort behaviour.
        """
        if not isinstance(event, PermissionAuditEvent):
            raise ValueError("Only PermissionAuditEvent values may be recorded")
        self._events.append(event)
        if self._sink is not None:
            try:
                with open(self._sink, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event.to_dict()) + "\n")
            except OSError as exc:
                self.sink_errors += 1
                if security_sensitive:
                    raise AuditSinkError(
                        "Security audit sink write failed; refusing to treat "
                        "the protected operation as successfully audited."
                    ) from exc
        return event

    def record_decision(self, *, agent: str, resource: str, operation: str,
                        decision: PolicyDecision | str,
                        security_sensitive: bool = False,
                        **fields: Any,
                        ) -> PermissionAuditEvent:
        """Build and record an event from keyword fields."""
        return self.record(
            PermissionAuditEvent(
                agent=agent, resource=resource, operation=operation,
                decision=decision, **fields),
            security_sensitive=security_sensitive)

    def record_security_decision(self, *, agent: str, resource: str,
                                 operation: str,
                                 decision: PolicyDecision | str,
                                 **fields: Any) -> PermissionAuditEvent:
        """Record a security-sensitive decision with fail-closed sink semantics."""
        return self.record_decision(
            agent=agent, resource=resource, operation=operation,
            decision=decision, security_sensitive=True, **fields)

    def record_evaluation(self, request: Any, evaluation: Any,
                          approval_id: str = "",
                          security_sensitive: bool = False,
                          ) -> PermissionAuditEvent:
        """Record a policy-engine ``(PermissionRequest, PermissionEvaluation)``."""
        return self.record_decision(
            agent=request.agent, resource=request.resource.value,
            operation=request.operation, scope=request.scope,
            risk=request.risk, decision=evaluation.decision,
            matched_rule=tuple(rule.id for rule in evaluation.matched_rules),
            reason=evaluation.reason, task_id=request.task_id,
            trace_id=request.trace_id, request_id=request.request_id,
            approval_required=(
                evaluation.decision == PolicyDecision.REQUIRE_APPROVAL),
            approval_id=approval_id,
            security_sensitive=security_sensitive)

    def record_security_evaluation(self, request: Any, evaluation: Any,
                                   approval_id: str = "") -> PermissionAuditEvent:
        """Record a policy evaluation as a security-sensitive audit event."""
        return self.record_evaluation(
            request, evaluation, approval_id=approval_id,
            security_sensitive=True)

    def query(self, *, task_id: str | None = None,
              decision: PolicyDecision | str | None = None,
              agent: str | None = None,
              resource: str | None = None) -> list[PermissionAuditEvent]:
        wanted = (decision.value if isinstance(decision, PolicyDecision)
                  else str(decision).upper() if decision is not None else None)
        return [event for event in self._events
                if (task_id is None or event.task_id == task_id)
                and (wanted is None or event.decision.value == wanted)
                and (agent is None or event.agent == agent)
                and (resource is None or event.resource == resource)]

    def to_dict(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self._events]
