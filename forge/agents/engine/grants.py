"""Permission grants (A81): the operator ledger, and the no-self-grant rule.

A specification declares a *ceiling*. Power is only live once an operator
records a grant for a specific operation, and the ledger enforces:

* **No self-grant.** The granting actor must be named and must not be the
  agent itself (nor an ``agent:<name>`` alias of it). An agent asking for
  its own permission is refused and the attempt is recorded.
* **No ceiling break.** Only operations the spec already declares can be
  granted. An operator cannot use the ledger to widen a spec behind the
  factory's back; that requires a new version of the spec.
* **Nothing blocked is grantable.** ``delete_repository`` and
  ``expose_secrets`` are refused for everyone.
* **Revocations are permanent records.** Revoking appends a revocation
  entry; history is never rewritten. The newest recorded decision wins,
  so only a *later* operator grant can restore access.

The ledger is a pure data structure over the package's ``grants.json``;
persistence belongs to :class:`~forge.agents.engine.package.PackageStore`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from forge.agents.engine.errors import AgentPermissionError
from forge.agents.engine.spec import (
    FORBIDDEN_OPERATIONS,
    GRANTABLE_OPERATIONS,
    AgentSpec,
)


def _actor_label(actor: str) -> str:
    return (actor or "").strip()


def is_self(actor: str, agent: str) -> bool:
    """True when *actor* is the agent itself under any known alias."""
    candidate = _actor_label(actor).lower()
    if not candidate:
        return False
    target = (agent or "").strip().lower()
    return candidate in (target, "agent:%s" % target, "agent-%s" % target)


@dataclass(frozen=True)
class Grant:
    """One operator grant of one operation to one agent."""

    agent: str
    operation: str
    granted_by: str
    granted_at: float
    scope: str = ""
    reason: str = ""
    expires_at: float = 0.0

    def active(self, now: float = 0.0) -> bool:
        moment = now or time.time()
        return bool(self.expires_at <= 0 or moment < self.expires_at)

    def to_dict(self) -> dict:
        return {
            "agent": self.agent, "operation": self.operation,
            "granted_by": self.granted_by, "granted_at": self.granted_at,
            "scope": self.scope, "reason": self.reason,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Grant":
        return cls(
            agent=str(payload.get("agent", "")),
            operation=str(payload.get("operation", "")),
            granted_by=str(payload.get("granted_by", "")),
            granted_at=float(payload.get("granted_at", 0.0)),
            scope=str(payload.get("scope", "")),
            reason=str(payload.get("reason", "")),
            expires_at=float(payload.get("expires_at", 0.0)))


@dataclass(frozen=True)
class Refusal:
    """A recorded refusal — the audit trail for attempts that failed."""

    agent: str
    operation: str
    actor: str
    reason: str
    at: float

    def to_dict(self) -> dict:
        return {"agent": self.agent, "operation": self.operation,
                "actor": self.actor, "reason": self.reason, "at": self.at}


class GrantLedger:
    """Grants and revocations for one agent package."""

    def __init__(self, agent: str, spec: AgentSpec,
                 payload: dict | None = None) -> None:
        self.agent = agent
        self.spec = spec
        payload = payload or {}
        self.grants: list = [Grant.from_dict(item)
                             for item in (payload.get("grants") or [])
                             if isinstance(item, dict)]
        self.revocations: list = [dict(item)
                                  for item in (payload.get("revocations") or [])
                                  if isinstance(item, dict)]
        self.refusals: list = [dict(item)
                               for item in (payload.get("refusals") or [])
                               if isinstance(item, dict)]

    # -- queries ---------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "grants": [grant.to_dict() for grant in self.grants],
            "revocations": list(self.revocations),
            "refusals": list(self.refusals[-50:]),
            "ceiling": list(self.spec.permissions.operations),
        }

    def active_operations(self, now: float = 0.0) -> tuple:
        """Operations currently granted, intersected with the spec ceiling.

        The most recent recorded decision wins: a revocation cancels every
        grant of that operation recorded before it, and only a *newer*
        operator grant can restore access. Nothing is ever deleted, so the
        ledger stays a complete audit trail.
        """
        ceiling = set(self.spec.permissions.operations)
        active = []
        for grant in self.grants:
            if grant.operation not in ceiling:
                continue  # a stale grant above a lowered ceiling is inert
            if not grant.active(now):
                continue
            if self._revoked_after(grant):
                continue
            active.append(grant.operation)
        return tuple(sorted(set(active)))

    def _revoked_after(self, grant: Grant) -> bool:
        """True when a revocation of this operation postdates the grant."""
        return any(entry.get("operation") == grant.operation
                   and float(entry.get("at", 0.0)) >= grant.granted_at
                   for entry in self.revocations)

    def holds(self, operation: str, now: float = 0.0) -> bool:
        return operation in self.active_operations(now)

    def is_granted(self, operation: str) -> bool:
        return any(grant.operation == operation for grant in self.grants)

    # -- mutation --------------------------------------------------------

    def grant(self, operation: str, *, actor: str, scope: str = "",
              reason: str = "", ttl_seconds: float = 0.0,
              now: float = 0.0) -> Grant:
        """Record an operator grant, or refuse and record the refusal."""
        moment = now or time.time()
        operation = (operation or "").strip()
        actor = _actor_label(actor)
        try:
            self._check_grant(operation, actor)
        except AgentPermissionError as exc:
            self._refuse(operation, actor, str(exc), moment)
            raise
        grant = Grant(
            agent=self.agent, operation=operation, granted_by=actor,
            granted_at=moment, scope=scope.strip(), reason=reason.strip(),
            expires_at=(moment + ttl_seconds) if ttl_seconds > 0 else 0.0)
        self.grants.append(grant)
        return grant

    def grant_spec_operations(self, *, actor: str, reason: str = "",
                              now: float = 0.0) -> list:
        """Grant every operation the spec declares (operator convenience).

        Still bounded by the spec: this can never add an operation the
        specification does not already declare.
        """
        created = []
        for operation in self.spec.permissions.operations:
            if not self.is_granted(operation):
                created.append(self.grant(
                    operation, actor=actor, reason=reason or
                    "granted from specification", now=now))
        return created

    def revoke(self, operation: str, *, actor: str, reason: str = "",
               now: float = 0.0) -> dict:
        actor = _actor_label(actor)
        if not actor:
            raise AgentPermissionError("A revocation needs an actor")
        operation = (operation or "").strip()
        if not self.is_granted(operation):
            raise AgentPermissionError(
                "Operation %r is not granted to %s" % (operation, self.agent))
        entry = {"operation": operation, "by": actor, "at": now or time.time(),
                 "reason": reason.strip()}
        self.revocations.append(entry)
        return entry

    # -- internals -------------------------------------------------------

    def _check_grant(self, operation: str, actor: str) -> None:
        if not operation:
            raise AgentPermissionError("An operation name is required")
        if operation in FORBIDDEN_OPERATIONS:
            raise AgentPermissionError(
                "Operation %r is permanently blocked for every agent"
                % operation)
        if operation not in GRANTABLE_OPERATIONS:
            raise AgentPermissionError(
                "Unknown permission operation: %r" % operation)
        if operation not in self.spec.permissions.operations:
            raise AgentPermissionError(
                "Operation %r is outside the declared ceiling for %s "
                "(ceiling: %s)"
                % (operation, self.agent,
                   ", ".join(self.spec.permissions.operations) or "empty"))
        if not actor:
            raise AgentPermissionError(
                "Grants require a named operator; anonymous grants are "
                "refused")
        if is_self(actor, self.agent):
            raise AgentPermissionError(
                "Agent %r cannot grant permissions to itself; an operator "
                "must grant %r" % (self.agent, operation))

    def _refuse(self, operation: str, actor: str, reason: str,
                at: float) -> None:
        self.refusals.append(Refusal(self.agent, operation, actor, reason,
                                     at).to_dict())
        self.refusals = self.refusals[-50:]
