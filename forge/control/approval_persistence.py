"""Durable approval-request persistence for server restart recovery."""
from __future__ import annotations

import json
from typing import Any

from forge.security.approvals import ApprovalRequest, ApprovalStatus

_TABLE_READY = "_forge_approval_persistence_ready"
_INSTALLED = "_forge_approval_persistence_installed"


def _ensure_schema(plane: Any) -> None:
    if getattr(plane, _TABLE_READY, False):
        return
    plane._db.execute(
        """
        CREATE TABLE IF NOT EXISTS approval_requests (
            id TEXT PRIMARY KEY,
            agent TEXT NOT NULL,
            resource TEXT NOT NULL,
            operation TEXT NOT NULL,
            scopes_json TEXT NOT NULL DEFAULT '[]',
            files_json TEXT NOT NULL DEFAULT '[]',
            task_id TEXT NOT NULL DEFAULT '',
            risk TEXT NOT NULL DEFAULT 'NONE',
            reason TEXT NOT NULL DEFAULT '',
            consequences TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            escalation INTEGER NOT NULL DEFAULT 0,
            bind_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            expires_at REAL,
            status TEXT NOT NULL,
            decided_by TEXT NOT NULL DEFAULT '',
            decided_at REAL
        )
        """
    )
    plane._db.execute(
        "CREATE INDEX IF NOT EXISTS idx_approval_requests_task "
        "ON approval_requests(task_id, status, created_at)"
    )
    setattr(plane, _TABLE_READY, True)


def _persist_request(plane: Any, request: ApprovalRequest) -> None:
    _ensure_schema(plane)
    bind = {name: value for name, value in request.bind}
    plane._db.execute(
        "INSERT OR REPLACE INTO approval_requests "
        "(id, agent, resource, operation, scopes_json, files_json, task_id, "
        "risk, reason, consequences, model, provider, escalation, bind_json, "
        "created_at, expires_at, status, decided_by, decided_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (request.id, request.agent, request.resource.value, request.operation,
         json.dumps(list(request.scopes), default=str),
         json.dumps(list(request.files), default=str), request.task_id,
         request.risk, request.reason, request.consequences, request.model,
         request.provider, int(request.escalation),
         json.dumps(bind, default=str), request.created_at,
         request.expires_at, request.status.value, request.decided_by,
         request.decided_at))


def _decode_list(value: Any) -> tuple[str, ...]:
    try:
        data = json.loads(value or "[]")
    except (TypeError, ValueError):
        return ()
    return tuple(str(item) for item in data) if isinstance(data, list) else ()


def _decode_bind(value: Any) -> tuple[tuple[str, Any], ...]:
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError):
        return ()
    if not isinstance(data, dict):
        return ()
    return tuple((str(name), item) for name, item in data.items())


def restore_approval_requests(plane: Any) -> dict[str, int]:
    """Load durable approval requests; never restores live approval tokens."""
    _ensure_schema(plane)
    if getattr(plane, _INSTALLED, False):
        return {"restored": len(plane.approval_store.all_requests())}
    rows = plane._db.query(
        "SELECT * FROM approval_requests ORDER BY created_at ASC LIMIT 10000"
    )
    restored = 0
    for row in rows:
        try:
            request = ApprovalRequest(
                agent=row["agent"], resource=row["resource"],
                operation=row["operation"],
                scopes=_decode_list(row["scopes_json"]),
                files=_decode_list(row["files_json"]),
                task_id=row["task_id"] or "", risk=row["risk"] or "NONE",
                reason=row["reason"] or "",
                consequences=row["consequences"] or "",
                model=row["model"] or "", provider=row["provider"] or "",
                escalation=bool(row["escalation"]),
                bind=_decode_bind(row["bind_json"]), id=row["id"],
                created_at=float(row["created_at"]),
                expires_at=row["expires_at"],
                status=ApprovalStatus(row["status"]),
                decided_by=row["decided_by"] or "",
                decided_at=row["decided_at"],
            )
            # Replace any stale process-local copy with durable state.
            plane.approval_store._requests[request.id] = request
            restored += 1
        except Exception:
            # Corrupt persisted approval records never become authority.
            continue

    store = plane.approval_store
    original_submit = store.submit
    original_decide = store.decide

    def submit(request: ApprovalRequest):
        result = original_submit(request)
        _persist_request(plane, result)
        return result

    def decide(request_id: str, approved: bool, decided_by: str):
        result = original_decide(request_id, approved, decided_by)
        _persist_request(plane, result)
        return result

    store.submit = submit
    store.decide = decide
    setattr(plane, _INSTALLED, True)
    return {"restored": restored}
