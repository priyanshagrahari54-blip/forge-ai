"""Compute API (A48): managed local code cells."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import Authed, authed_mutation, get_plane, rate_limit
from forge.api.schemas import ComputeExecuteRequest
from forge.control.control_plane import (ApprovalConflictError,
                                         ApprovalNotFoundError,
                                         ControlPlane, InvalidRequest,
                                         TaskNotFound)

router = APIRouter()


@router.post("/compute/execute", dependencies=[rate_limit("compute")])
async def execute(body: ComputeExecuteRequest,
                  current: Authed = Depends(authed_mutation),
                  plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.compute_execute(current.session, body.code,
                                     timeout=body.timeout,
                                     approval_id=body.approval_id)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/compute/status")
async def status(current: Authed = Depends(authed_mutation),
                 plane: ControlPlane = Depends(get_plane)):
    return plane.compute_status(current.session)


@router.get("/compute/history")
async def history(current: Authed = Depends(authed_mutation),
                  plane: ControlPlane = Depends(get_plane)):
    return plane.compute_history(current.session)


@router.get("/compute/approvals")
async def approvals(current: Authed = Depends(authed_mutation),
                    plane: ControlPlane = Depends(get_plane)):
    return {"approvals": plane.list_compute_approvals(current.session)}


@router.post("/compute/approvals/{approval_id}/approve",
             dependencies=[rate_limit("compute")])
async def approve(approval_id: str,
                  current: Authed = Depends(authed_mutation),
                  plane: ControlPlane = Depends(get_plane)):
    return _decide(plane, current.session, approval_id, True)


@router.post("/compute/approvals/{approval_id}/deny",
             dependencies=[rate_limit("compute")])
async def deny(approval_id: str,
               current: Authed = Depends(authed_mutation),
               plane: ControlPlane = Depends(get_plane)):
    return _decide(plane, current.session, approval_id, False)


def _decide(plane: ControlPlane, session, approval_id: str,
            approved: bool) -> dict:
    try:
        return plane.decide_compute_approval(session, approval_id, approved)
    except (TaskNotFound, ApprovalNotFoundError):
        raise HTTPException(status_code=404, detail="Not found") from None
    except ApprovalConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
