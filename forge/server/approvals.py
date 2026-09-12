"""Durable approval requests bridged to the A33 approval store (A81).

When a task hits a permission boundary (the Supervisor's change-set or
commit gate yields ``REQUIRE_APPROVAL``), the worker files an approval
request. Requests are persisted in SQLite — so a Forge Desktop client
that reconnects later still sees every pending decision — and mirrored
into the in-memory A33 :class:`~forge.security.approvals.ApprovalStore`,
which is the only component that can mint redeemable approval tokens.

Authority semantics are exactly A33's:

* deciding ``approved`` records the operator, then the *waiting worker*
  mints a scoped, TTL-bounded, use-count-bounded token bound to the
  change-set fingerprint;
* denying, expiring, or timing out returns ``""`` and the run fails
  closed (the Supervisor rolls its candidate set back);
* an agent can never approve its own request;
* after a server restart pending approvals are expired honestly — the
  interrupted task re-queues and asks again if it still needs a decision.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from forge.security.approvals import ApprovalStatus, ApprovalStore
from forge.security.policy import Resource, risk_rank, translate_a32
from forge.server.errors import (
    ApprovalConflict,
    ApprovalExpired,
    ApprovalNotFound,
    InvalidRequest,
    PermissionDenied,
)
from forge.server.storage import Database


class ApprovalManager:
    """Persistent approvals + A33 token minting + waiter wakeup."""

    def __init__(self, db: Database, store: ApprovalStore, *,
                 emit: Callable[..., Any],
                 notify: Callable[..., Any],
                 approval_timeout: float = 600.0,
                 token_ttl: float = 300.0) -> None:
        self._db = db
        self.store = store
        self._emit = emit
        self._notify = notify
        self.approval_timeout = float(approval_timeout)
        self.token_ttl = float(token_ttl)
        self._lock = threading.RLock()
        self._waiters: Dict[str, threading.Event] = {}

    # -- filing ------------------------------------------------------------------

    def file_query(self, task: Any, query: Any, *,
                   model: str = "", provider: str = "") -> List[Any]:
        """File A33 requests for a Supervisor :class:`ApprovalQuery`.

        Returns the filed A33 requests (normally exactly one). Raises
        :class:`InvalidRequest` when the query cannot be represented —
        callers fail closed.
        """
        from forge.security.approvals import ApprovalRequest

        groups: Dict[Any, List[Any]] = {}
        order: List[Any] = []
        for item in query.items:
            translated = translate_a32(item.operation)
            if translated is None:
                raise InvalidRequest(
                    "Operation %r is outside approval scope."
                    % item.operation)
            key = (translated[0].value, translated[1])
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(item)
        filed = []
        for key in order:
            resource_value, operation = key
            items = groups[key]
            resource = Resource(resource_value)
            paths = [item.path for item in items if item.path]
            risk = "NONE"
            for item in items:
                if risk_rank(item.risk) > risk_rank(risk):
                    risk = (item.risk or "NONE").upper()
            reasons = "; ".join(dict.fromkeys(
                item.reason for item in items if item.reason))
            # Bind the A33 request to the enforcement-side task id: the
            # Supervisor files queries under its own internal run id, and
            # the token minted here is redeemed with that same id. The
            # server's task id stays on the persisted row for API reads.
            bind_task_id = str(getattr(query, "task_id", "") or "") \
                or task.task_id
            request = ApprovalRequest(
                agent=query.agent, resource=resource, operation=operation,
                scopes=tuple(paths) if resource == Resource.FILESYSTEM
                else (),
                files=tuple(paths), task_id=bind_task_id, risk=risk,
                reason=(reasons[:1000] or
                        "Operator decision needed for %s" % operation),
                consequences=self._consequences(operation, paths, query),
                model=model, provider=provider,
                expires_at=time.time() + self.approval_timeout)
            self.store.submit(request)
            self._persist(task, request, query, fingerprint=query.fingerprint)
            self._emit(
                task.task_id, task.project_id, "approval.required",
                {"approval_id": request.id, "agent": request.agent,
                 "operation": "%s:%s" % (resource.value, operation),
                 "paths": paths, "risk": request.risk,
                 "reason": request.reason,
                 "consequences": request.consequences,
                 "expires_at": request.expires_at,
                 "label": query.label})
            self._notify(
                task.project_id, "approval.requested",
                "Approval needed for task %s" % task.task_id,
                "%s wants %s:%s (%d path(s))."
                % (request.agent, resource.value, operation, len(paths)),
                task_id=task.task_id)
            filed.append(request)
        return filed

    @staticmethod
    def _consequences(operation: str, paths: List[str], query: Any) -> str:
        if operation == "commit":
            return ("Authorizes committing the accepted candidate files "
                    "to git for this task only.")
        listed = ", ".join(paths[:10])
        if len(paths) > 10:
            listed += " (+%d more)" % (len(paths) - 10)
        return ("Authorizes %s of %d file(s) for this task only: %s"
                % (query.label or "applying", len(paths), listed)).rstrip()

    def _persist(self, task: Any, request: Any, query: Any, *,
                 fingerprint: str) -> None:
        payload = {
            "operation": "%s:%s" % (request.resource.value,
                                    request.operation),
            "paths": list(request.files or request.scopes),
            "risk": request.risk,
            "reason": request.reason,
            "consequences": request.consequences,
            "label": getattr(query, "label", ""),
            "capability": getattr(query, "capability", ""),
            "agent": request.agent,
        }
        self._db.execute(
            "INSERT INTO approvals (id, task_id, project_id, agent, kind, "
            "payload, fingerprint, status, decided_by, token_id, "
            "created_at, expires_at, decided_at) VALUES (?, ?, ?, ?, ?, "
            "?, ?, 'pending', '', '', ?, ?, NULL)",
            (request.id, task.task_id, task.project_id, request.agent,
             "change_set", json.dumps(payload, default=str),
             fingerprint or "", request.created_at,
             request.expires_at or (time.time() + self.approval_timeout)))

    # -- reads ---------------------------------------------------------------------

    def get(self, approval_id: str) -> Optional[Dict[str, Any]]:
        row = self._db.query_one(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,))
        return self._row_to_dict(row) if row is not None else None

    def get_or_raise(self, approval_id: str) -> Dict[str, Any]:
        record = self.get(approval_id)
        if record is None:
            raise ApprovalNotFound(
                "Unknown approval: %r" % approval_id,
                approval_id=approval_id)
        return record

    def pending(self, project_id: str = "") -> List[Dict[str, Any]]:
        sql = ("SELECT * FROM approvals WHERE status = 'pending' "
               "AND expires_at > ?")
        params: List[Any] = [time.time()]
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        sql += " ORDER BY created_at ASC"
        return [self._row_to_dict(row) for row in self._db.query(sql, params)]

    def list_for_task(self, task_id: str) -> List[Dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM approvals WHERE task_id = ? ORDER BY "
            "created_at ASC", (task_id,))
        return [self._row_to_dict(row) for row in rows]

    # -- deciding ---------------------------------------------------------------------

    def decide(self, approval_id: str, approved: bool, decided_by: str, *,
               project_id: str = "") -> "tuple[Dict[str, Any], bool]":
        """Record an operator decision; returns (record, duplicate)."""
        if not decided_by or not isinstance(decided_by, str):
            raise InvalidRequest("A decision needs a named approver.")
        with self._lock:
            record = self.get_or_raise(approval_id)
            if project_id and record["project_id"] != project_id:
                raise PermissionDenied(
                    "Approval is not visible from this project.")
            request = self.store.get_request(approval_id)
            if record["status"] != "pending":
                expected = "approved" if approved else "denied"
                if (record["status"] == expected
                        and record["decided_by"] == decided_by):
                    return record, True  # idempotent retry
                raise ApprovalConflict(
                    "Approval is already %s." % record["status"])
            if record["agent"] == decided_by:
                raise PermissionDenied(
                    "An agent cannot approve its own request.")
            if time.time() >= float(record["expires_at"]):
                self._set_status(approval_id, "expired")
                if request is not None:
                    request.status = ApprovalStatus.EXPIRED
                self._emit(record["task_id"], record["project_id"],
                           "approval.expired",
                           {"approval_id": approval_id})
                raise ApprovalExpired("Approval request has expired.")
            if request is not None:
                # A33 is the authority: it re-checks self-approval,
                # expiry, and double decisions.
                self.store.decide(approval_id, approved, decided_by)
            else:
                # Server restarted since filing: the in-memory A33 request
                # is gone, so no token can be minted. Fail closed.
                self._set_status(approval_id, "expired")
                raise ApprovalExpired(
                    "Approval predates this server boot and can no longer "
                    "mint tokens; the task will re-ask if it still needs "
                    "a decision.")
            status = "approved" if approved else "denied"
            self._db.execute(
                "UPDATE approvals SET status = ?, decided_by = ?, "
                "decided_at = ? WHERE id = ?",
                (status, decided_by, time.time(), approval_id))
            record = self.get_or_raise(approval_id)
            self._emit(
                record["task_id"], record["project_id"],
                "approval.%s" % status,
                {"approval_id": approval_id, "decided_by": decided_by,
                 "operation": record["payload"].get("operation", "")})
            self._notify(
                record["project_id"], "approval.%s" % status,
                "Approval %s for task %s" % (status, record["task_id"]),
                "Decided by %s." % decided_by, task_id=record["task_id"])
            self._wake(approval_id)
            return record, False

    def _set_status(self, approval_id: str, status: str) -> None:
        self._db.execute(
            "UPDATE approvals SET status = ?, decided_at = ? WHERE id = ?",
            (status, time.time(), approval_id))

    # -- waiting (worker thread) --------------------------------------------------------

    def wait_for_token(self, requests: List[Any], *, control: Any = None,
                       fingerprint: str = "",
                       timeout: Optional[float] = None) -> str:
        """Block until every request is decided; mint and return one token.

        Returns ``""`` when denied, expired, mixed-operation, or timed
        out (fail closed). Raises :class:`TaskCancelled` when the task is
        cancelled while waiting.
        """
        from forge.core.run_control import TaskCancelled

        if len(requests) != 1:
            for request in requests:
                self._wake(request.id)
            return ""
        request = requests[0]
        event = threading.Event()
        with self._lock:
            self._waiters[request.id] = event
        limit = self.approval_timeout if timeout is None else timeout
        deadline = time.time() + max(1.0, float(limit))
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
                    record = self.get(request.id) or {}
                    self._set_status(request.id, "expired")
                    self._emit(
                        request.task_id,
                        str(record.get("project_id", "")),
                        "approval.expired", {"approval_id": request.id})
                return ""
            uses = len(request.files) if request.files else \
                len(request.scopes)
            try:
                token = self.store.issue(
                    request.id, request.decided_by,
                    ttl_seconds=self.token_ttl,
                    max_uses=max(1, uses),
                    fingerprint=fingerprint or "")
            except ValueError:
                return ""
            self._db.execute(
                "UPDATE approvals SET token_id = ? WHERE id = ?",
                (token.id, request.id))
            return token.id
        finally:
            with self._lock:
                self._waiters.pop(request.id, None)

    def _wake(self, approval_id: str) -> None:
        with self._lock:
            event = self._waiters.get(approval_id)
        if event is not None:
            event.set()

    # -- restart hygiene -------------------------------------------------------------------

    def expire_stale(self) -> int:
        """Startup/expiry sweep: mark overdue pending rows expired."""
        cursor = self._db.execute(
            "UPDATE approvals SET status = 'expired', decided_at = ? "
            "WHERE status = 'pending' AND expires_at <= ?",
            (time.time(), time.time()))
        return cursor.rowcount

    def expire_all_pending(self, reason: str) -> int:
        """Startup: pending approvals cannot outlive their server boot."""
        rows = self._db.query(
            "SELECT id, task_id, project_id FROM approvals "
            "WHERE status = 'pending'")
        for row in rows:
            self._emit(row["task_id"], row["project_id"],
                       "approval.expired",
                       {"approval_id": row["id"], "reason": reason})
        cursor = self._db.execute(
            "UPDATE approvals SET status = 'expired', decided_at = ? "
            "WHERE status = 'pending'", (time.time(),))
        return cursor.rowcount

    # -- serialization ---------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: Any) -> Dict[str, Any]:
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            payload = {}
        return {
            "approval_id": row["id"],
            "task_id": row["task_id"],
            "project_id": row["project_id"],
            "agent": row["agent"],
            "kind": row["kind"],
            "payload": payload,
            "fingerprint": row["fingerprint"],
            "status": row["status"],
            "decided_by": row["decided_by"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "decided_at": row["decided_at"],
        }
