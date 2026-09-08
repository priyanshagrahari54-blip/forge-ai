"""Computer-use API (A40): vision-driven, policy-gated control.

Screen → understand → element tree → propose → gate → execute, all
through the existing A33 policy/approval/audit systems. SAFE/LOCKED
sessions may only observe; HIGH/CRITICAL risk escalates to operator
approval; confirmation dialogs fail closed; a per-task action budget
caps executed actions; history is redacted (typed text never leaves
the provider boundary).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import Authed, authed, authed_mutation, get_plane, \
    rate_limit
from forge.api.schemas import ComputerActRequest, ComputerScreenRequest
from forge.control.control_plane import (ApprovalConflictError,
                                         ApprovalNotFoundError,
                                         ControlPlane, InvalidRequest,
                                         PolicyDenied, TaskNotFound)

router = APIRouter()


@router.post("/computer/observe", dependencies=[rate_limit("computer")])
async def computer_observe(body: ComputerScreenRequest,
                           current: Authed = Depends(authed_mutation),
                           plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.computer_observe(current.session, body.image_b64,
                                      goal=body.goal)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except PolicyDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None


@router.post("/computer/propose", dependencies=[rate_limit("computer")])
async def computer_propose(body: ComputerScreenRequest,
                           current: Authed = Depends(authed_mutation),
                           plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.computer_propose(current.session, body.image_b64,
                                      goal=body.goal)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except PolicyDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None


@router.post("/computer/act", dependencies=[rate_limit("computer")])
async def computer_act(body: ComputerActRequest,
                       current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.computer_act(
            current.session, body.action, target=body.target,
            params=body.params, reason=body.reason,
            approval_id=body.approval_id)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/computer/cycle", dependencies=[rate_limit("computer")])
async def computer_cycle(body: ComputerScreenRequest,
                         current: Authed = Depends(authed_mutation),
                         plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.computer_cycle(current.session, body.image_b64,
                                    goal=body.goal)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except PolicyDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None


@router.get("/computer/history")
async def computer_history(current: Authed = Depends(authed),
                           plane: ControlPlane = Depends(get_plane)):
    return plane.computer_history(current.session)


@router.get("/computer/approvals")
async def computer_approvals(current: Authed = Depends(authed),
                             plane: ControlPlane = Depends(get_plane)):
    return {"approvals": plane.list_computer_approvals(current.session)}


@router.post("/computer/approvals/{approval_id}/approve",
             dependencies=[rate_limit("computer")])
async def computer_approve(approval_id: str,
                           current: Authed = Depends(authed_mutation),
                           plane: ControlPlane = Depends(get_plane)):
    return _decide(plane, current.session, approval_id, True)


@router.post("/computer/approvals/{approval_id}/deny",
             dependencies=[rate_limit("computer")])
async def computer_deny(approval_id: str,
                        current: Authed = Depends(authed_mutation),
                        plane: ControlPlane = Depends(get_plane)):
    return _decide(plane, current.session, approval_id, False)


def _decide(plane: ControlPlane, session, approval_id: str,
            approved: bool) -> dict:
    try:
        return plane.decide_computer_approval(session, approval_id,
                                              approved)
    except (TaskNotFound, ApprovalNotFoundError):
        raise HTTPException(status_code=404, detail="Not found") from None
    except ApprovalConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
