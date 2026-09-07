"""Approval adaptation between the cockpit and A33 (A34).

This module is an ADAPTER, not a second permission system. Authority
lives entirely in :class:`forge.security.approvals.ApprovalStore`:

- approval requests are filed as A33 :class:`ApprovalRequest` values;
- decisions go through :meth:`ApprovalStore.decide` (distinct approver,
  expiry, single transition — enforced by A33, re-checked here);
- granted authority is a scoped, single-purpose, time-bounded A33 token
  minted with :meth:`ApprovalStore.issue` and redeemed on the normal
  enforcement path.

What the adapter adds: filing requests from interactive
:class:`ApprovalQuery <forge.tools.change_applier.ApprovalQuery>` values,
blocking the worker until the browser decides (or a timeout/cancel
intervenes), project-scope checks, and event/audit fan-out.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from forge.security.approvals import (
    ApprovalRequest,
    ApprovalStatus,
    ApprovalStore,
)
from forge.security.policy import Resource, risk_rank, translate_a32


class ApprovalError(Exception):
    """Base error for approval adapter failures (fail closed)."""


class ApprovalNotFound(ApprovalError):
    pass


class ApprovalForbidden(ApprovalError):
    pass


class ApprovalConflict(ApprovalError):
    pass


class ApprovalExpired(ApprovalError):
    pass


class ApprovalService:
    """File, track, and decide A33 approvals for cockpit runs."""

    def __init__(self, store: ApprovalStore, *,
                 resolve_run: Callable[[str], Any | None],
                 emit: Callable[..., Any],
                 approval_timeout: float = 600.0,
                 token_ttl: float = 300.0) -> None:
        self.store = store
        self._resolve_run = resolve_run
        self._emit = emit
        self.approval_timeout = approval_timeout
        self.token_ttl = token_ttl
        # RLock: decide() wakes waiters while holding the lock.
        self._lock = threading.RLock()
        self._waiters: dict[str, threading.Event] = {}

    # -- filing -----------------------------------------------------------

    def file_query(self, query: Any, run: Any, *,
                   model: str = "", provider: str = "") -> list[ApprovalRequest]:
        """File one A33 request per (resource, operation) group in a query.

        Returns the filed requests (normally exactly one). Raises
        :class:`ApprovalError` when the query cannot be represented —
        callers fail closed.
        """
        groups: dict[tuple[str, str], list[Any]] = {}
        order: list[tuple[str, str]] = []
        for item in query.items:
            translated = translate_a32(item.operation)
            if translated is None:
                raise ApprovalError(
                    f"Operation {item.operation!r} is outside approval scope")
            resource, engine_operation = translated
            key = (resource.value, engine_operation)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(item)
        now = time.time()
        filed: list[ApprovalRequest] = []
        for key in order:
            resource_value, engine_operation = key
            items = groups[key]
            resource = Resource(resource_value)
            paths = [item.path for item in items if item.path]
            risk = "NONE"
            for item in items:
                if risk_rank(item.risk) > risk_rank(risk):
                    risk = (item.risk or "NONE").upper()
            reasons = "; ".join(
                dict.fromkeys(item.reason for item in items if item.reason))
            try:
                request = ApprovalRequest(
                    agent=query.agent, resource=resource,
                    operation=engine_operation,
                    scopes=tuple(paths) if resource == Resource.FILESYSTEM else (),
                    files=tuple(paths),
                    task_id=query.task_id, risk=risk,
                    reason=(reasons[:1000] or
                            f"Operator decision needed for {engine_operation}"),
                    consequences=self._consequences(
                        engine_operation, paths, query),
                    model=model, provider=provider,
                    expires_at=now + self.approval_timeout)
            except ValueError as exc:
                raise ApprovalError(f"Cannot file approval: {exc}") from exc
            self.store.submit(request)
            filed.append(request)
            self._emit(
                run.id, run.project_id, "approval.required",
                {"approval_id": request.id,
                 "agent": request.agent,
                 "task_id": run.id,
                 "tool": items[0].tool if items else "",
                 "operation": f"{resource.value}:{engine_operation}",
                 "paths": paths,
                 "scope": list(request.scopes),
                 "risk": request.risk,
                 "reason": request.reason,
                 "consequences": request.consequences,
                 "model": model, "provider": provider,
                 "expires_at": request.expires_at,
                 "label": query.label})
        return filed

    @staticmethod
    def _consequences(operation: str, paths: list[str], query: Any) -> str:
        if operation == "commit":
            return ("Authorizes committing the accepted candidate files to "
                    "git for this task only.")
        listed = ", ".join(paths[:10])
        if len(paths) > 10:
            listed += f" (+{len(paths) - 10} more)"
        return (f"Authorizes {query.label or 'applying'} {len(paths)} "
                f"file(s) for this task only: {listed}".rstrip())

    def file_rollback(self, run: Any, files: list[str], *,
                      checkpoint_id: str) -> ApprovalRequest:
        """File a scoped request authorizing one rollback."""
        try:
            request = ApprovalRequest(
                agent="cockpit-rollback", resource=Resource.FILESYSTEM,
                operation="write", scopes=tuple(files), files=tuple(files),
                task_id=run.id, risk="MEDIUM",
                reason=f"Rollback task {run.id} to checkpoint {checkpoint_id}",
                consequences=(
                    f"Restores {len(files)} candidate file(s) from the "
                    f"pre-run checkpoint; unrelated files stay untouched."),
                expires_at=time.time() + self.approval_timeout)
        except ValueError as exc:
            raise ApprovalError(f"Cannot file rollback approval: {exc}") from exc
        self.store.submit(request)
        self._emit(
            run.id, run.project_id, "approval.required",
            {"approval_id": request.id, "agent": request.agent,
             "task_id": run.id, "tool": "rollback",
             "operation": "filesystem:write", "paths": list(files),
             "scope": list(request.scopes), "risk": request.risk,
             "reason": request.reason,
             "consequences": request.consequences,
             "expires_at": request.expires_at, "label": "rollback"})
        return request

    # -- waiting (worker thread) ------------------------------------------

    def wait_for_token(self, requests: list[ApprovalRequest], *,
                       control: Any = None,
                       fingerprint: str = "",
                       timeout: float | None = None) -> str:
        """Block until every request is decided; mint and return one token.

        Returns ``""`` when denied, expired, mixed-operation, or timed out
        (fail closed). Raises ``TaskCancelled`` when the run is cancelled
        while waiting.
        """
        from forge.core.run_control import TaskCancelled

        if len(requests) != 1:
            # Mixed-operation change sets cannot be covered by one token;
            # fail closed rather than partially authorize.
            for request in requests:
                self._wake(request.id)
            return ""
        request = requests[0]
        event = threading.Event()
        with self._lock:
            self._waiters[request.id] = event
        limit = self.approval_timeout if timeout is None else timeout
        deadline = time.time() + max(1.0, limit)
        try:
            while True:
                if control is not None and control.cancel_requested:
                    raise TaskCancelled("cancelled while awaiting approval")
                if event.wait(timeout=0.1):
                    break
                if time.time() >= deadline:
                    break
                if request.status != ApprovalStatus.PENDING:
                    break
            if request.status != ApprovalStatus.APPROVED:
                if request.status == ApprovalStatus.PENDING:
                    run = self._resolve_run(request.task_id)
                    if run is not None:
                        self._emit(
                            run.id, run.project_id, "approval.expired",
                            {"approval_id": request.id,
                             "task_id": run.id})
                return ""
            uses = len(request.files) if request.files else len(request.scopes)
            try:
                token = self.store.issue(
                    request.id, request.decided_by,
                    ttl_seconds=self.token_ttl,
                    max_uses=max(1, uses),
                    fingerprint=fingerprint or "")
            except ValueError:
                return ""
            return token.id
        finally:
            with self._lock:
                self._waiters.pop(request.id, None)

    def _wake(self, request_id: str) -> None:
        with self._lock:
            event = self._waiters.get(request_id)
        if event is not None:
            event.set()

    # -- decisions (API thread) -------------------------------------------

    def get(self, request_id: str) -> ApprovalRequest | None:
        return self.store.get_request(request_id)

    def pending_for_project(self, project_id: str) -> list[ApprovalRequest]:
        visible: list[ApprovalRequest] = []
        for request in self.store.pending():
            run = self._resolve_run(request.task_id)
            if run is not None and run.project_id == project_id:
                visible.append(request)
        return visible

    def decide(self, request_id: str, approved: bool, decided_by: str, *,
               project_id: str) -> tuple[ApprovalRequest, bool]:
        """Record a decision; returns (request, duplicate).

        Raises :class:`ApprovalNotFound`, :class:`ApprovalForbidden`,
        :class:`ApprovalConflict`, or :class:`ApprovalExpired`.
        """
        with self._lock:
            request = self.store.get_request(request_id)
            if request is None:
                raise ApprovalNotFound(f"Unknown approval: {request_id!r}")
            run = self._resolve_run(request.task_id)
            if run is None or run.project_id != project_id:
                raise ApprovalForbidden(
                    "Approval is not visible from this project")
            if decided_by == request.agent:
                raise ApprovalForbidden(
                    "An agent cannot approve its own request")
            if request.status != ApprovalStatus.PENDING:
                expected = (ApprovalStatus.APPROVED if approved
                            else ApprovalStatus.DENIED)
                if (request.status == expected
                        and request.decided_by == decided_by):
                    return request, True  # idempotent retry
                raise ApprovalConflict(
                    f"Request is already {request.status.value}")
            if request.expired(self.store.now()):
                request.status = ApprovalStatus.EXPIRED
                self._emit(run.id, run.project_id, "approval.expired",
                           {"approval_id": request.id,
                            "task_id": run.id})
                raise ApprovalExpired("Approval request has expired")
            try:
                decided = self.store.decide(request_id, approved, decided_by)
            except ValueError as exc:
                raise ApprovalConflict(str(exc)) from exc
            self._emit(
                run.id, run.project_id,
                "approval.approved" if approved else "approval.denied",
                {"approval_id": request.id, "task_id": run.id,
                 "decided_by": decided_by,
                 "operation": f"{request.resource.value}:{request.operation}",
                 "paths": list(request.files or request.scopes)})
            self._wake(request.id)
            return decided, False
