"""Multi-agent orchestration API (A38).

Submits requirements as coordinated agent teams: the control plane
plans a capability-matched team, dispatches steps through the A33
``Resource.AGENT`` policy gate (with operator approvals where required),
and records a full per-step report with evidence. Records are strictly
session-scoped at this boundary.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from forge.api.deps import Authed, authed, authed_mutation, get_plane, \
    rate_limit
from forge.api.schemas import OrchestrationSubmitRequest
from forge.control.control_plane import (ApprovalConflictError,
                                         ApprovalNotFoundError,
                                         ControlPlane, InvalidRequest,
                                         TaskNotFound)

router = APIRouter()


def _record(record: Any) -> dict[str, Any]:
    return record.to_dict(include_report=True)


@router.post("/orchestrations", dependencies=[rate_limit("orchestrations")])
async def submit_orchestration(body: OrchestrationSubmitRequest,
                               current: Authed = Depends(authed_mutation),
                               plane: ControlPlane = Depends(get_plane)):
    record = plane.submit_orchestration(
        current.session, body.requirement, chain=body.chain)
    return _record(record)


@router.get("/orchestrations")
async def list_orchestrations(current: Authed = Depends(authed),
                              plane: ControlPlane = Depends(get_plane)):
    records = plane.list_orchestrations(current.session)
    return {"orchestrations": [record.to_dict() for record in records]}


@router.get("/orchestrations/{orchestration_id}")
async def get_orchestration(orchestration_id: str,
                            current: Authed = Depends(authed),
                            plane: ControlPlane = Depends(get_plane)):
    from fastapi import HTTPException

    try:
        record = plane.get_orchestration(current.session, orchestration_id)
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="Not found") from None
    return _record(record)


@router.post("/orchestrations/{orchestration_id}/cancel",
             dependencies=[rate_limit("orchestrations")])
async def cancel_orchestration(orchestration_id: str,
                               current: Authed = Depends(authed_mutation),
                               plane: ControlPlane = Depends(get_plane)):
    from fastapi import HTTPException

    try:
        record = plane.cancel_orchestration(current.session,
                                            orchestration_id)
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="Not found") from None
    except InvalidRequest as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _record(record)


@router.get("/orchestrations/{orchestration_id}/approvals")
async def list_orchestration_approvals(
        orchestration_id: str, current: Authed = Depends(authed),
        plane: ControlPlane = Depends(get_plane)):
    from fastapi import HTTPException

    try:
        approvals = plane.list_orchestration_approvals(
            current.session, orchestration_id)
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="Not found") from None
    return {"approvals": approvals}


@router.post(
    "/orchestrations/{orchestration_id}/approvals/{approval_id}/approve",
    dependencies=[rate_limit("orchestrations")])
async def approve(orchestration_id: str, approval_id: str,
                  current: Authed = Depends(authed_mutation),
                  plane: ControlPlane = Depends(get_plane)):
    return _decide(plane, current.session, orchestration_id, approval_id,
                   True)


@router.post(
    "/orchestrations/{orchestration_id}/approvals/{approval_id}/deny",
    dependencies=[rate_limit("orchestrations")])
async def deny(orchestration_id: str, approval_id: str,
               current: Authed = Depends(authed_mutation),
               plane: ControlPlane = Depends(get_plane)):
    return _decide(plane, current.session, orchestration_id, approval_id,
                   False)


def _decide(plane: ControlPlane, session: Any, orchestration_id: str,
            approval_id: str, approved: bool) -> dict[str, Any]:
    from fastapi import HTTPException

    try:
        result = plane.decide_orchestration_approval(
            session, orchestration_id, approval_id, approved)
    except (TaskNotFound, ApprovalNotFoundError):
        raise HTTPException(status_code=404, detail="Not found") from None
    except ApprovalConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return result
