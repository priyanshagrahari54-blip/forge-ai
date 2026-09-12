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

#: Every scope the server understands. Closed set.
SCOPES: FrozenSet[str] = frozenset({
    "tasks:read", "tasks:write", "tasks:control", "tasks:rollback",
    "approvals:read", "approvals:decide",
    "projects:read", "projects:write",
    "events:read", "logs:read", "results:read",
    "health:read", "status:read",
    "notifications:read", "notifications:write",
    "sessions:write", "keys:write", "recovery:read",
})

_READ_SCOPES: FrozenSet[str] = frozenset({
    "tasks:read", "approvals:read", "projects:read", "events:read",
    "logs:read", "results:read", "health:read", "status:read",
    "notifications:read", "recovery:read"})

#: Role → scopes. Roles are closed; unknown roles fail at key creation.
ROLE_SCOPES: Dict[str, FrozenSet[str]] = {
    "admin": SCOPES,
    "operator": frozenset(SCOPES - {"keys:write"}),
    "viewer": _READ_SCOPES,
}

#: Closed API operation table: operation → required scope. Every route
#: declares exactly one of these; no operation accepts executable text.
API_OPERATIONS: Dict[str, str] = {
    "task.create": "tasks:write",
    "task.read": "tasks:read",
    "task.list": "tasks:read",
    "task.pause": "tasks:control",
    "task.resume": "tasks:control",
    "task.cancel": "tasks:control",
    "task.retry": "tasks:control",
    "task.rollback": "tasks:rollback",
    "task.logs": "logs:read",
    "task.result": "results:read",
    "task.events": "events:read",
    "approval.list": "approvals:read",
    "approval.decide": "approvals:decide",
    "project.register": "projects:write",
    "project.list": "projects:read",
    "health.read": "health:read",
    "status.read": "status:read",
    "notifications.list": "notifications:read",
    "notifications.read": "notifications:write",
    "session.create": "sessions:write",
    "session.revoke": "sessions:write",
    "session.list": "sessions:write",
    "key.create": "keys:write",
    "key.revoke": "keys:write",
    "recovery.read": "recovery:read",
}

#: Request fields that would turn the API into an execution surface.
#: Rejected on sight wherever free-form payloads are accepted.
EXECUTION_VECTOR_FIELDS: FrozenSet[str] = frozenset({
    "command", "cmd", "shell", "script", "exec", "execute", "argv",
    "stdin", "program", "binary", "subprocess", "eval", "python",
    "powershell", "bash", "cmdline"})

#: Server permission profiles, mapped to A33 policy factories and modes.
PROFILES: FrozenSet[str] = frozenset({
    "safe", "assisted", "autonomous", "locked"})

#: Repository-relative probe path used for task-admission evaluation.
#: It represents "some write inside the project root"; A33 filesystem
#: scopes are always root-relative, never absolute host paths.
_ADMISSION_PROBE_SCOPE = "forge-server/admission-probe"

_PROFILE_MODES: Dict[str, OperationMode] = {
    "safe": OperationMode.SAFE,
    "assisted": OperationMode.ASSISTED,
    "autonomous": OperationMode.AUTONOMOUS,
    "locked": OperationMode.LOCKED,
}

#: Mode restrictiveness (lower = stricter). A task mode can only ever be
#: clamped *down* to the server profile — never up.
MODE_RANK: Dict[OperationMode, int] = {
    OperationMode.LOCKED: 0,
    OperationMode.SAFE: 1,
    OperationMode.ASSISTED: 2,
    OperationMode.AUTONOMOUS: 3,
}


def policy_for_profile(profile: str) -> PermissionPolicy:
    """Build the A33 :class:`PermissionPolicy` for a server profile."""
    if profile == "safe":
        return safe_profile()
    if profile == "assisted":
        return assisted_profile()
    if profile == "autonomous":
        return autonomous_profile()
    if profile == "locked":
        return locked_profile()
    raise InvalidRequest("Unknown permission profile: %r" % profile)


def mode_for_profile(profile: str) -> OperationMode:
    return _PROFILE_MODES.get(profile, OperationMode.ASSISTED)


def clamp_mode(requested: Any, profile_mode: OperationMode) -> OperationMode:
    """Clamp a requested task mode so it is never looser than the profile."""
    if requested in (None, ""):
        return profile_mode
    try:
        mode = OperationMode(str(requested).lower())
    except ValueError:
        raise InvalidRequest(
            "Unknown mode %r; want safe|assisted|autonomous|locked."
            % requested) from None
    if MODE_RANK[mode] > MODE_RANK[profile_mode]:
        return profile_mode
    return mode


def reject_execution_vectors(payload: "Dict[str, Any]") -> None:
    """Refuse payloads carrying execution-shaped fields (defense in depth).

    The closed schemas already forbid unknown fields; this guard makes
    the remote-shell guarantee explicit even if a future schema drifts.
    """
    if not isinstance(payload, dict):
        return
    for key in payload:
        if str(key).lower() in EXECUTION_VECTOR_FIELDS:
            raise InvalidRequest(
                "Field %r is not accepted: the Forge Server API never "
                "executes client-supplied commands. Submit a task "
                "requirement instead." % key)


class Authorizer:
    """Scope checks plus A33 task-admission policy."""

    def __init__(self, profile: str = "assisted", *,
                 policy: Optional[PermissionPolicy] = None) -> None:
        if profile not in PROFILES:
            raise InvalidRequest("Unknown permission profile: %r" % profile)
        self.profile = profile
        self.profile_mode = mode_for_profile(profile)
        #: An explicit policy overrides the profile-built one (custom A33
        #: rules); admission and runs use this single instance.
        self.policy = policy if policy is not None else \
            policy_for_profile(profile)

    # -- API scope layer ------------------------------------------------------

    @staticmethod
    def scopes_for_role(role: str) -> FrozenSet[str]:
        try:
            return ROLE_SCOPES[role]
        except KeyError:
            raise InvalidRequest("Unknown role: %r" % role) from None

    @staticmethod
    def scope_for_operation(operation: str) -> str:
        try:
            return API_OPERATIONS[operation]
        except KeyError:
            # Fail closed: an unmapped operation is a server bug, not a
            # permission grant.
            raise PermissionDenied(
                "Operation %r is not part of the Forge Server API."
                % operation) from None

    def require(self, principal: Any, operation_or_scope: str) -> None:
        """Authorize a principal for an API operation (or a raw scope)."""
        scope = (self.scope_for_operation(operation_or_scope)
                 if operation_or_scope in API_OPERATIONS
                 else operation_or_scope)
        if scope not in SCOPES:
            raise PermissionDenied("Unknown scope: %r" % scope)
        if scope not in getattr(principal, "scopes", frozenset()):
            raise PermissionDenied(
                "Principal %r lacks scope %r."
                % (getattr(principal, "name", "?"), scope),
                required_scope=scope)

    # -- A33 execution layer ----------------------------------------------------

    def ensure_task_permitted(self, project_root: str, mode: OperationMode,
                              *, project_id: str = "",
                              audit: Any = None) -> None:
        """Task admission against the A33 policy (fail closed).

        Evaluates one representative filesystem-write request inside the
        project root (A33 filesystem scopes are repository-relative —
        the Supervisor binds every real write to the same root). ``DENY``
        (the default for ``safe``/``locked`` profiles and for any custom
        policy without a write ALLOW) blocks task creation with
        ``POLICY_DENIED`` — a task that could never write anything must
        not occupy the queue. ``REQUIRE_APPROVAL``/``ALLOW`` admit it;
        individual writes are still re-checked by the gate at run time.

        Every admission decision (allow *and* deny) is recorded in the
        A33 audit log when one is provided.
        """
        del project_root  # the probe scope is repo-relative by contract
        if (mode in (OperationMode.LOCKED, OperationMode.SAFE)
                or self.profile in ("locked", "safe")):
            if audit is not None:
                audit.record_decision(
                    agent="forge-server", resource="filesystem",
                    operation="write", decision=PolicyDecision.DENY,
                    scope=_ADMISSION_PROBE_SCOPE, risk="LOW",
                    reason="%s profile/mode: task admission refused"
                           % (self.profile or mode.value))
            raise PolicyDenied(
                "The '%s' profile/mode is read-only; the engineering "
                "pipeline requires writes, so tasks cannot run. Use the "
                "assisted or autonomous profile."
                % (self.profile if self.profile in ("locked", "safe")
                   else mode.value))
        request = PermissionRequest(
            agent="forge-server", resource=Resource.FILESYSTEM,
            operation="write", scope=_ADMISSION_PROBE_SCOPE, risk="LOW",
            reason="Forge Server task admission for project %r"
                   % (project_id or "?"),
            task_id="")
        evaluation = self.policy.evaluate(request)
        if audit is not None:
            try:
                audit.record_evaluation(request, evaluation)
            except Exception:
                pass  # auditing must never break admission
        if evaluation.decision == PolicyDecision.DENY:
            raise PolicyDenied(
                "Active permission profile '%s' denies filesystem writes "
                "inside the project (%s). Use the assisted or autonomous "
                "profile, or attach a custom policy."
                % (self.profile, evaluation.reason))

    def operation_mode(self, requested: Any = "") -> OperationMode:
        """Effective task mode: requested, clamped to the server profile."""
        return clamp_mode(requested, self.profile_mode)

    def describe(self) -> "Dict[str, Any]":
        rules: Iterable[Any] = ()
        try:
            rules = self.policy.rules
        except Exception:
            rules = ()
        return {
            "profile": self.profile,
            "mode": self.profile_mode.value,
            "policy_rules": [rule.to_dict() for rule in rules],
            "api_operations": dict(API_OPERATIONS),
            "scopes": sorted(SCOPES),
            "roles": {role: sorted(scopes)
                      for role, scopes in ROLE_SCOPES.items()},
        }
