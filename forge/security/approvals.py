"""Approval requests, tokens, and task-scoped grants (A33).

When policy returns ``REQUIRE_APPROVAL``, Forge produces a structured
:class:`ApprovalRequest` explaining the operation, resource, scope, agent,
risk, and consequences — and stops. A distinct approver (never the requesting
agent itself) may mint a scoped :class:`ApprovalToken`:

- **scoped**: bound to agent, task, resource, operation, scopes, and optional
  detail bindings (ports, args, providers) — a token for ``write src/foo.py``
  authorizes nothing else;
- **explicit**: minted only from an approved request by a named approver;
- **auditable**: every token cites its request, approver, and validity window;
- **non-transferable**: redeemable only by the bound agent;
- **time-bounded**: expired tokens are invalid automatically and silently
  expiring grants are never extended;
- **replay-resistant**: single-use by default (``max_uses`` is explicit).

:class:`TaskGrant` gives a task temporary authority over exactly its declared
files; grants expire with the task and are revoked on rollback. Agents can
request escalation through :meth:`ApprovalStore.request_escalation`, but an
agent can never grant itself permission: issuance requires an approver
identity distinct from the requester.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable
from uuid import uuid4

from forge.security.policy import (
    RESOURCE_OPERATIONS,
    PermissionRequest,
    Resource,
    _host_of_url,
    _match_domain_pattern,
    _match_fs_pattern,
    validate_scope,
)


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass
class ApprovalRequest:
    """A structured question to the operator. Forge stops until decided."""

    agent: str
    resource: Resource | str
    operation: str
    scopes: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    task_id: str = ""
    risk: str = "NONE"
    reason: str = ""
    consequences: str = ""
    model: str = ""
    provider: str = ""
    escalation: bool = False
    #: Suggested detail bindings (port, args, provider, ...) carried onto the
    #: minted token so redemption re-checks them.
    bind: tuple[tuple[str, Any], ...] = ()
    id: str = field(default_factory=lambda: uuid4().hex)
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_by: str = ""
    decided_at: float | None = None

    def __post_init__(self) -> None:
        resource = Resource(self.resource)
        object.__setattr__(self, "resource", resource)
        operation = (self.operation or "").lower()
        if operation not in RESOURCE_OPERATIONS[resource]:
            raise ValueError(
                f"Unknown operation {self.operation!r} for {resource.value!r}")
        object.__setattr__(self, "operation", operation)
        if not self.agent or not isinstance(self.agent, str):
            raise ValueError("Approval request needs a named agent")
        normalized = tuple(validate_scope(resource, scope)
                           for scope in self.scopes)
        object.__setattr__(self, "scopes", normalized)
        object.__setattr__(self, "risk", (self.risk or "NONE").upper())

    def expired(self, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        return self.expires_at is not None and moment >= self.expires_at

    def describe(self) -> str:
        """Render the human-readable approval prompt."""
        lines = [
            "APPROVAL REQUIRED",
            "",
            "Agent:",
            self.agent,
            "",
            "Operation:",
            f"{self.operation} ({self.resource.value})",
            "",
            "Scope:",
            *(self.scopes if self.scopes else ("(none)",)),
            "",
        ]
        if self.files:
            lines += ["Files:", *self.files, ""]
        lines += ["Risk:", self.risk, ""]
        if self.bind:
            lines += ["Bindings:", *(
                f"{name} = {value!r}" for name, value in self.bind), ""]
        if self.model or self.provider:
            lines += ["Model:", f"{self.model} ({self.provider})".strip(), ""]
        lines += ["Reason:", self.reason or "(no reason given)", ""]
        if self.consequences:
            lines += ["Consequences:", self.consequences, ""]
        if self.escalation:
            lines += ["Escalation: this request exceeds current privileges.", ""]
        lines += [f"Approve? (request id: {self.id})"]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "agent": self.agent,
            "resource": self.resource.value,
            "operation": self.operation,
            "scopes": list(self.scopes),
            "files": list(self.files),
            "risk": self.risk,
            "reason": self.reason,
            "consequences": self.consequences,
            "model": self.model,
            "provider": self.provider,
            "escalation": self.escalation,
            "bind": {name: value for name, value in self.bind},
            "status": self.status.value,
            "decided_by": self.decided_by,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class ApprovalToken:
    """Scoped, explicit, non-transferable, time-bounded authorization."""

    agent: str
    task_id: str
    resource: Resource
    operation: str
    scopes: tuple[str, ...]
    files: tuple[str, ...]
    issued_by: str
    issued_at: float
    expires_at: float
    max_uses: int = 1
    fingerprint: str = ""
    bind: tuple[tuple[str, Any], ...] = ()
    request_id: str = ""
    id: str = field(default_factory=lambda: uuid4().hex)

    def expired(self, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        return moment >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "request_id": self.request_id,
            "agent": self.agent,
            "task_id": self.task_id,
            "resource": self.resource.value,
            "operation": self.operation,
            "scopes": list(self.scopes),
            "files": list(self.files),
            "fingerprint": self.fingerprint,
            "bind": {name: value for name, value in self.bind},
            "issued_by": self.issued_by,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "max_uses": self.max_uses,
        }


@dataclass(frozen=True)
class TaskGrant:
    """Temporary task authority over exactly its declared files."""

    task_id: str
    files: tuple[str, ...]
    fingerprint: str
    issued_at: float
    expires_at: float
    id: str = field(default_factory=lambda: uuid4().hex)

    def expired(self, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        return moment >= self.expires_at

    def covers(self, path: str) -> bool:
        return path in self.files

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "files": list(self.files),
            "fingerprint": self.fingerprint,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }


@dataclass
class _TokenRecord:
    token: ApprovalToken
    uses: int = 0
    revoked: bool = False


@dataclass
class _GrantRecord:
    grant: TaskGrant
    revoked: bool = False


def _scope_covers(resource: Resource, granted: str,
                  request: PermissionRequest) -> bool:
    """True when a granted scope contains the requested scope (no expansion)."""
    if resource == Resource.FILESYSTEM:
        return _match_fs_pattern(granted, request.scope)
    if resource == Resource.BROWSER:
        host = _host_of_url(request.scope)
        return host is not None and _match_domain_pattern(granted, host)
    if resource == Resource.NETWORK:
        host = request.detail("host", "")
        return bool(host) and _match_domain_pattern(granted, str(host))
    if resource == Resource.TERMINAL:
        # Exact executable only at redeem time — no basename leniency, no
        # wildcards (forbidden at request validation).
        return granted == request.scope
    # model / git / desktop / voice: exact scope, empty covers any target.
    if not granted:
        return True
    return granted == request.scope


def enforce_with_token(store: "ApprovalStore | None", token_id: str,
                       request: PermissionRequest,
                       fingerprint: str = "") -> tuple[bool, str]:
    """Redeem a token for a ``REQUIRE_APPROVAL`` outcome on an enforcement path.

    Missing store or token fails closed with an explanatory reason; token
    bindings recorded at issuance are re-checked by :meth:`ApprovalStore.redeem`.
    """
    if not token_id:
        return False, "Approval required before executing this operation."
    if store is None:
        return False, "Approval required, and no approval store is configured."
    return store.redeem(token_id, request, fingerprint=fingerprint)


class ApprovalStore:
    """Mint, redeem, and revoke approvals and task grants."""

    def __init__(self, clock: Callable[[], float] | None = None,
                 request_ttl: float = 3600.0) -> None:
        self._clock = clock or time.time
        self.request_ttl = request_ttl
        self._requests: dict[str, ApprovalRequest] = {}
        self._tokens: dict[str, _TokenRecord] = {}
        self._grants: dict[str, _GrantRecord] = {}

    def now(self) -> float:
        return self._clock()

    # -- requests -----------------------------------------------------------

    def submit(self, request: ApprovalRequest) -> ApprovalRequest:
        if not isinstance(request, ApprovalRequest):
            raise ValueError("Only ApprovalRequest values may be submitted")
        if request.expires_at is None:
            request.expires_at = self.now() + self.request_ttl
        self._requests[request.id] = request
        return request

    def get_request(self, request_id: str) -> ApprovalRequest | None:
        return self._requests.get(request_id)

    def pending(self) -> list[ApprovalRequest]:
        return [request for request in self._requests.values()
                if request.status == ApprovalStatus.PENDING
                and not request.expired(self.now())]

    def decide(self, request_id: str, approved: bool,
               decided_by: str) -> ApprovalRequest:
        """Record the operator's decision. The approver must differ from the
        requesting agent — agents cannot approve themselves."""
        request = self._requests.get(request_id)
        if request is None:
            raise ValueError(f"Unknown approval request: {request_id!r}")
        if not decided_by or not isinstance(decided_by, str):
            raise ValueError("A decision needs a named approver")
        if decided_by == request.agent:
            raise ValueError("An agent cannot approve its own request")
        if request.status != ApprovalStatus.PENDING:
            raise ValueError(
                f"Request {request_id!r} is already {request.status.value}")
        if request.expired(self.now()):
            request.status = ApprovalStatus.EXPIRED
            raise ValueError(f"Request {request_id!r} has expired")
        request.status = (ApprovalStatus.APPROVED if approved
                          else ApprovalStatus.DENIED)
        request.decided_by = decided_by
        request.decided_at = self.now()
        return request

    def request_escalation(self, *, agent: str, task_id: str,
                           resource: Resource | str, operation: str,
                           scopes: tuple[str, ...], reason: str,
                           current: str) -> ApprovalRequest:
        """File an escalation request. Minting still needs another approver."""
        return self.submit(ApprovalRequest(
            agent=agent, task_id=task_id, resource=resource,
            operation=operation, scopes=scopes, reason=reason,
            consequences=("Current privileges: " + (current or "none") +
                          ". Granting exceeds them for this task only."),
            escalation=True))

    # -- tokens ---------------------------------------------------------------

    def issue(self, request_id: str, decided_by: str, *,
              ttl_seconds: float = 600.0, max_uses: int = 1,
              fingerprint: str = "",
              bind: tuple[tuple[str, Any], ...] = ()) -> ApprovalToken:
        """Mint a token from an approved request. Only the recorded approver's
        decision mints, and issuance never extends the request's own scope."""
        request = self._requests.get(request_id)
        if request is None:
            raise ValueError(f"Unknown approval request: {request_id!r}")
        if request.status != ApprovalStatus.APPROVED:
            raise ValueError(
                f"Request {request_id!r} is {request.status.value}, not approved")
        if decided_by != request.decided_by:
            raise ValueError("Only the recorded approver's decision mints a token")
        if max_uses < 1:
            raise ValueError("max_uses must be at least 1")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        issued_at = self.now()
        merged_bind = tuple(request.bind) + tuple(bind)
        token = ApprovalToken(
            agent=request.agent, task_id=request.task_id,
            resource=request.resource, operation=request.operation,
            scopes=request.scopes, files=request.files,
            issued_by=decided_by, issued_at=issued_at,
            expires_at=issued_at + ttl_seconds, max_uses=max_uses,
            fingerprint=fingerprint, bind=merged_bind,
            request_id=request.id)
        self._tokens[token.id] = _TokenRecord(token=token)
        return token

    def redeem(self, token_id: str, request: PermissionRequest, *,
               fingerprint: str = "",
               now: float | None = None) -> tuple[bool, str]:
        """Validate a token against an actual request, consuming one use.

        Returns ``(allowed, reason)``; failures are closed, never raised,
        because redemption happens on the enforcement path.
        """
        moment = self.now() if now is None else now
        record = self._tokens.get(token_id)
        if record is None:
            return False, "Unknown approval token"
        token = record.token
        if record.revoked:
            return False, "Approval token was revoked"
        if token.expired(moment):
            return False, "Approval token has expired"
        if record.uses >= token.max_uses:
            return False, "Approval token has already been used"
        if request.agent != token.agent:
            return False, "Approval token is bound to another agent"
        if token.task_id and request.task_id != token.task_id:
            return False, "Approval token is bound to another task"
        if request.resource != token.resource or request.operation != token.operation:
            return False, "Approval token covers a different operation"
        if token.fingerprint and fingerprint != token.fingerprint:
            return False, "Approval token is bound to a different proposal"
        for name, value in token.bind:
            if request.detail(name) != value:
                return False, f"Approval token binding {name!r} does not match"
        if token.resource == Resource.FILESYSTEM and token.files:
            if request.scope not in token.files:
                return False, "Approval token does not cover this file"
        if not any(_scope_covers(token.resource, granted, request)
                   for granted in token.scopes):
            return False, "Approval token does not cover this scope"
        record.uses += 1
        return True, f"Approved by {token.issued_by} (request {token.request_id})"

    def revoke_token(self, token_id: str) -> bool:
        record = self._tokens.get(token_id)
        if record is None:
            return False
        record.revoked = True
        return True

    # -- task grants ------------------------------------------------------------

    def grant_task(self, task_id: str, files: tuple[str, ...] | list[str],
                   fingerprint: str, ttl_seconds: float = 3600.0) -> TaskGrant:
        if not task_id:
            raise ValueError("Task grants need a task id")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        issued_at = self.now()
        grant = TaskGrant(task_id=task_id, files=tuple(files),
                          fingerprint=fingerprint, issued_at=issued_at,
                          expires_at=issued_at + ttl_seconds)
        self._grants[grant.id] = _GrantRecord(grant=grant)
        return grant

    def check_task_grant(self, task_id: str, path: str, fingerprint: str,
                         now: float | None = None) -> tuple[bool, str]:
        """True when a live grant covers ``path`` for ``task_id``."""
        moment = self.now() if now is None else now
        for record in self._grants.values():
            grant = record.grant
            if grant.task_id != task_id or record.revoked:
                continue
            if grant.expired(moment):
                continue
            if grant.fingerprint != fingerprint:
                continue
            if grant.covers(path):
                return True, f"Task grant {grant.id} covers {path}"
        return False, f"No live task grant covers {path} for task {task_id}"

    def revoke_task(self, task_id: str) -> int:
        """Revoke every token and grant bound to a task (task end/rollback)."""
        count = 0
        for record in self._tokens.values():
            if record.token.task_id == task_id and not record.revoked:
                record.revoked = True
                count += 1
        for record in self._grants.values():
            if record.grant.task_id == task_id and not record.revoked:
                record.revoked = True
                count += 1
        return count

    def active_grants(self, task_id: str) -> list[TaskGrant]:
        moment = self.now()
        return [record.grant for record in self._grants.values()
                if record.grant.task_id == task_id and not record.revoked
                and not record.grant.expired(moment)]

    def prune(self) -> int:
        """Drop expired tokens and grants. Never extends anything."""
        moment = self.now()
        before = len(self._tokens) + len(self._grants)
        self._tokens = {key: record for key, record in self._tokens.items()
                        if not record.token.expired(moment)}
        self._grants = {key: record for key, record in self._grants.items()
                        if not record.grant.expired(moment)}
        for request in self._requests.values():
            if (request.status == ApprovalStatus.PENDING
                    and request.expired(moment)):
                request.status = ApprovalStatus.EXPIRED
        return before - (len(self._tokens) + len(self._grants))
