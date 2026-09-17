"""Authorization: scopes, roles, and the A33 policy bridge (A81).

Two layers decide whether something may happen:

1. **API authorization** — every request carries a principal (API key or
   session) with a role; roles map to a closed scope set, and every API
   operation requires exactly one scope. Anything not in the scope
   table fails closed with 403.

2. **Execution authorization (A33)** — what a *task* may do on disk is
   decided by the existing A33 permission platform
   (:mod:`forge.security.policy` / :mod:`forge.security.permissions`).
   The server profile (``safe``/``assisted``/``autonomous``/``locked``)
   builds a real :class:`PermissionPolicy`; task admission evaluates a
   filesystem-write request against it, and the Supervisor run re-checks
   every individual write through the same engine. The server never
   loosens an A33 verdict — it can only refuse earlier.

Remote-shell guarantee
----------------------
The Forge Server API is **not** a remote shell, by construction:

* The operation set is closed (:data:`API_OPERATIONS`). There is no
  operation — and no route — that accepts a command, script, argv, or
  code payload for direct execution.
* Task requirements are *data* for the Supervisor pipeline. They are
  never interpreted, ``eval``-ed, or passed to a subprocess by the
  server itself; every disk effect flows through the policy-gated
  Supervisor transaction (checkpoint → change set → gate → approval →
  tests → verification → acceptance → exact-file commit).
* Payloads with execution-shaped fields are rejected
  (:func:`reject_execution_vectors`), schemas forbid unknown fields,
  and ids/paths fail closed on anything malformed.
* A profile whose A33 policy denies writes (``safe``/``locked`` or a
  custom DENY rule) blocks task admission outright.
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, Iterable, Optional

from forge.security.permissions import OperationMode
from forge.security.policy import (
    PermissionPolicy,
    PermissionRequest,
    Resource,
    assisted_profile,
    autonomous_profile,
    locked_profile,
    safe_profile,
)
from forge.security.policy_gate import PolicyDecision
from forge.server.errors import InvalidRequest, PermissionDenied, PolicyDenied

SCOPES: FrozenSet[str] = frozenset({
    "tasks:read", "tasks:write", "tasks:control", "tasks:rollback",
    "approvals:read", "approvals:decide",
    "projects:read", "projects:write",
    "events:read", "logs:read", "results:read",
    "health:read", "status:read",
    "notifications:read", "notifications:write",
    "sessions:write", "keys:write", "recovery:read",
    "models:read", "models:control", "inference:run", "inference:control",
})

_READ_SCOPES: FrozenSet[str] = frozenset({
    "tasks:read", "approvals:read", "projects:read", "events:read",
    "logs:read", "results:read", "health:read", "status:read",
    "notifications:read", "recovery:read", "models:read"})

ROLE_SCOPES: Dict[str, FrozenSet[str]] = {
    "admin": SCOPES,
    "operator": frozenset(SCOPES - {"keys:write"}),
    "viewer": _READ_SCOPES,
}

API_OPERATIONS: Dict[str, str] = {
    "task.create": "tasks:write", "task.read": "tasks:read",
    "task.list": "tasks:read", "task.pause": "tasks:control",
    "task.resume": "tasks:control", "task.cancel": "tasks:control",
    "task.retry": "tasks:control", "task.rollback": "tasks:rollback",
    "task.logs": "logs:read", "task.result": "results:read",
    "task.events": "events:read", "approval.list": "approvals:read",
    "approval.decide": "approvals:decide", "project.register": "projects:write",
    "project.list": "projects:read", "health.read": "health:read",
    "status.read": "status:read", "notifications.list": "notifications:read",
    "notifications.read": "notifications:write", "session.create": "sessions:write",
    "session.revoke": "sessions:write", "session.list": "sessions:write",
    "key.create": "keys:write", "key.revoke": "keys:write",
    "recovery.read": "recovery:read", "models.list": "models:read",
    "models.status": "models:read", "models.verify": "models:control",
    "models.load": "models:control", "models.unload": "models:control",
    "inference.generate": "inference:run", "inference.stream": "inference:run",
    "inference.stream_events": "inference:run", "inference.cancel": "inference:control",
    "inference.status": "status:read",
}

EXECUTION_VECTOR_FIELDS: FrozenSet[str] = frozenset({
    "command", "cmd", "shell", "script", "exec", "execute", "argv",
    "stdin", "program", "binary", "subprocess", "eval", "python",
    "powershell", "bash", "cmdline"})

PROFILES: FrozenSet[str] = frozenset({"safe", "assisted", "autonomous", "locked"})
_ADMISSION_PROBE_SCOPE = "forge-server/admission-probe"
_PROFILE_MODES: Dict[str, OperationMode] = {
    "safe": OperationMode.SAFE, "assisted": OperationMode.ASSISTED,
    "autonomous": OperationMode.AUTONOMOUS, "locked": OperationMode.LOCKED,
}
MODE_RANK: Dict[OperationMode, int] = {
    OperationMode.LOCKED: 0, OperationMode.SAFE: 1,
    OperationMode.ASSISTED: 2, OperationMode.AUTONOMOUS: 3,
}


def policy_for_profile(profile: str) -> PermissionPolicy:
    if profile == "safe": return safe_profile()
    if profile == "assisted": return assisted_profile()
    if profile == "autonomous": return autonomous_profile()
    if profile == "locked": return locked_profile()
    raise InvalidRequest("Unknown permission profile: %r" % profile)


def mode_for_profile(profile: str) -> OperationMode:
    return _PROFILE_MODES.get(profile, OperationMode.ASSISTED)


def clamp_mode(requested: Any, profile_mode: OperationMode) -> OperationMode:
    if requested in (None, ""): return profile_mode
    try:
        mode = OperationMode(str(requested).lower())
    except ValueError:
        raise InvalidRequest(
            "Unknown mode %r; want safe|assisted|autonomous|locked." % requested) from None
    if MODE_RANK[mode] > MODE_RANK[profile_mode]: return profile_mode
    return mode


def reject_execution_vectors(payload: "Dict[str, Any]") -> None:
    if not isinstance(payload, dict): return
    for key in payload:
        if str(key).lower() in EXECUTION_VECTOR_FIELDS:
            raise InvalidRequest(
                "Field %r is not accepted: the Forge Server API never executes "
                "client-supplied commands. Submit a task requirement instead." % key)


class Authorizer:
    """Scope checks plus A33 task-admission policy."""

    def __init__(self, profile: str = "assisted", *,
                 policy: Optional[PermissionPolicy] = None) -> None:
        if profile not in PROFILES:
            raise InvalidRequest("Unknown permission profile: %r" % profile)
        self.profile = profile
        self.profile_mode = mode_for_profile(profile)
        self.policy = policy if policy is not None else policy_for_profile(profile)

    @staticmethod
    def scopes_for_role(role: str) -> FrozenSet[str]:
        try: return ROLE_SCOPES[role]
        except KeyError: raise InvalidRequest("Unknown role: %r" % role) from None

    @staticmethod
    def scope_for_operation(operation: str) -> str:
        try: return API_OPERATIONS[operation]
        except KeyError:
            raise PermissionDenied(
                "Operation %r is not part of the Forge Server API." % operation) from None

    def require(self, principal: Any, operation_or_scope: str) -> None:
        scope = (self.scope_for_operation(operation_or_scope)
                 if operation_or_scope in API_OPERATIONS else operation_or_scope)
        if scope not in SCOPES:
            raise PermissionDenied("Unknown scope: %r" % scope)
        if scope not in getattr(principal, "scopes", frozenset()):
            raise PermissionDenied(
                "Principal %r lacks scope %r." %
                (getattr(principal, "name", "?"), scope), required_scope=scope)

    def ensure_task_permitted(self, project_root: str, mode: OperationMode,
                              *, project_id: str = "", audit: Any = None) -> None:
        """Task admission against A33; security audit failure is fail-closed."""
        del project_root
        if (mode in (OperationMode.LOCKED, OperationMode.SAFE)
                or self.profile in ("locked", "safe")):
            if audit is not None:
                if hasattr(audit, "record_security_decision"):
                    audit.record_security_decision(
                        agent="forge-server", resource="filesystem",
                        operation="write", decision=PolicyDecision.DENY,
                        scope=_ADMISSION_PROBE_SCOPE, risk="LOW",
                        reason="%s profile/mode: task admission refused" %
                               (self.profile or mode.value))
                else:
                    audit.record_decision(
                        agent="forge-server", resource="filesystem",
                        operation="write", decision=PolicyDecision.DENY,
                        scope=_ADMISSION_PROBE_SCOPE, risk="LOW",
                        reason="%s profile/mode: task admission refused" %
                               (self.profile or mode.value))
            raise PolicyDenied(
                "The '%s' profile/mode is read-only; the engineering pipeline "
                "requires writes, so tasks cannot run. Use the assisted or "
                "autonomous profile." %
                (self.profile if self.profile in ("locked", "safe") else mode.value))
        request = PermissionRequest(
            agent="forge-server", resource=Resource.FILESYSTEM,
            operation="write", scope=_ADMISSION_PROBE_SCOPE, risk="LOW",
            reason="Forge Server task admission for project %r" % (project_id or "?"),
            task_id="")
        evaluation = self.policy.evaluate(request)
        if audit is not None:
            if hasattr(audit, "record_security_evaluation"):
                audit.record_security_evaluation(request, evaluation)
            else:
                # Legacy audit implementations are still allowed, but we do
                # not swallow their failure: admission must not pass while a
                # security decision is unaudited.
                audit.record_evaluation(request, evaluation)
        if evaluation.decision == PolicyDecision.DENY:
            raise PolicyDenied(
                "Active permission profile '%s' denies filesystem writes inside "
                "the project (%s). Use the assisted or autonomous profile, or "
                "attach a custom policy." % (self.profile, evaluation.reason))

    def operation_mode(self, requested: Any = "") -> OperationMode:
        return clamp_mode(requested, self.profile_mode)

    def describe(self) -> "Dict[str, Any]":
        rules: Iterable[Any] = ()
        try: rules = self.policy.rules
        except Exception: rules = ()
        return {
            "profile": self.profile, "mode": self.profile_mode.value,
            "policy_rules": [rule.to_dict() for rule in rules],
            "api_operations": dict(API_OPERATIONS), "scopes": sorted(SCOPES),
            "roles": {role: sorted(scopes) for role, scopes in ROLE_SCOPES.items()},
        }
