"""Parallel task execution API (A81).

Submits dependency-aware task graphs for concurrent execution: the
control plane validates the graph, dispatches ready, conflict-free
tasks through the A33 permission gate (with operator approvals where
required), and records per-task outcomes, structured messages, and the
live agent-activity snapshot the cockpit renders. Records are strictly
session-scoped at this boundary.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from forge.api.deps import (Authed, authed, authed_mutation, get_plane,
                            rate_limit)
from forge.api.schemas import ExecutionSubmitRequest
from forge.control.control_plane import (ControlPlane, InvalidRequest,
                                         TaskNotFound)

router = APIRouter()


def _record(record: Any) -> Dict[str, Any]:
    return record.to_dict(include_report=True)


@router.post("/executions", dependencies=[rate_limit("executions")])
async def submit_execution(body: ExecutionSubmitRequest,
                           current: Authed = Depends(authed_mutation),
                           plane: ControlPlane = Depends(get_plane)):
    record = plane.submit_execution(
        current.session, body.requirement,
        [task.model_dump() for task in body.tasks],
        max_workers=body.max_workers, mode=body.mode)
    return _record(record)


@router.get("/executions")
async def list_executions(current: Authed = Depends(authed),
                          plane: ControlPlane = Depends(get_plane)):
    records = plane.list_executions(current.session)
    return {"executions": [record.to_dict() for record in records]}


@router.get("/executions/{execution_id}")
async def get_execution(execution_id: str,
                        current: Authed = Depends(authed),
                        plane: ControlPlane = Depends(get_plane)):
    from fastapi import HTTPException

    try:
        record = plane.get_execution(current.session, execution_id)
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="Not found") from None
    return _record(record)


@router.post("/executions/{execution_id}/cancel",
             dependencies=[rate_limit("executions")])
async def cancel_execution(execution_id: str,
                           current: Authed = Depends(authed_mutation),
                           plane: ControlPlane = Depends(get_plane)):
    from fastapi import HTTPException

    try:
        record = plane.cancel_execution(current.session, execution_id)
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="Not found") from None
    except InvalidRequest as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _record(record)


@router.get("/executions/{execution_id}/approvals")
async def list_execution_approvals(execution_id: str,
                                   current: Authed = Depends(authed),
                                   plane: ControlPlane = Depends(get_plane)):
    from fastapi import HTTPException

    try:
        record = plane.get_execution(current.session, execution_id)
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="Not found") from None
    from forge.control.control_plane import InvalidRequest

    visible = []
    for request in plane.approval_store.pending():
        if request.task_id == record.id:
            visible.append(request.to_dict())
    return {"approvals": visible}


@router.post("/executions/{execution_id}/approvals/{approval_id}/decision")
async def decide_execution_approval(execution_id: str, approval_id: str,
                                    approved: bool,
                                    current: Authed = Depends(authed_mutation),
                                    plane: ControlPlane = Depends(get_plane)):
    """Decide one approval filed by an execution (A33 flow)."""
    from fastapi import HTTPException

    from forge.control.control_plane import (ApprovalConflictError,
                                             ApprovalNotFoundError)

    try:
        record = plane.get_execution(current.session, execution_id)
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="Not found") from None
    request = plane.approval_store.get_request(approval_id)
    if request is None or request.task_id != record.id:
        raise HTTPException(status_code=404,
                            detail="Unknown approval")
    try:
        decided = plane.approval_store.decide(
            approval_id, approved, current.session.actor)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    token_id = ""
    if approved:
        token = plane.approval_store.issue(
            approval_id, decided_by=current.session.actor,
            ttl_seconds=plane.config.approval_token_ttl, max_uses=1)
        token_id = token.id
    # Wake the waiting worker thread (shared A33 approval machinery).
    with plane._orch_lock:
        event = plane._orch_events.get(approval_id)
    if event is not None:
        event.set()
    plane._audit(current.session.actor, "execution",
                 "approve" if approved else "deny", True,
                 task_id=record.id,
                 reason=f"execution approval {approval_id} "
                        f"{'approved' if approved else 'denied'}")
    return {"approval": decided.to_dict(), "token_id": token_id}
