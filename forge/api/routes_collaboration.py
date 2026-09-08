"""AI-to-AI collaboration API (A44): gated consultations with external
AIs; responses are always marked untrusted."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import Authed, authed_mutation, get_plane, rate_limit
from forge.api.schemas import CollaborationConsultRequest
from forge.control.control_plane import (ApprovalConflictError,
                                         ApprovalNotFoundError,
                                         ControlPlane, InvalidRequest,
                                         TaskNotFound)

router = APIRouter()


@router.get("/ai-to-ai/capabilities")
async def capabilities(current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    return plane.collaboration_capabilities(current.session)


@router.post("/ai-to-ai/consult", dependencies=[rate_limit("ai-to-ai")])
async def consult(body: CollaborationConsultRequest,
                  current: Authed = Depends(authed_mutation),
                  plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.collaboration_consult(
            current.session, body.question, provider=body.provider,
            approval_id=body.approval_id)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/ai-to-ai/history")
async def history(current: Authed = Depends(authed_mutation),
                  plane: ControlPlane = Depends(get_plane)):
    return plane.collaboration_history(current.session)


@router.get("/ai-to-ai/approvals")
async def approvals(current: Authed = Depends(authed_mutation),
                    plane: ControlPlane = Depends(get_plane)):
    return {"approvals": plane.list_collaboration_approvals(
        current.session)}


@router.post("/ai-to-ai/approvals/{approval_id}/approve",
             dependencies=[rate_limit("ai-to-ai")])
async def approve(approval_id: str,
                  current: Authed = Depends(authed_mutation),
                  plane: ControlPlane = Depends(get_plane)):
    return _decide(plane, current.session, approval_id, True)


@router.post("/ai-to-ai/approvals/{approval_id}/deny",
             dependencies=[rate_limit("ai-to-ai")])
async def deny(approval_id: str,
               current: Authed = Depends(authed_mutation),
               plane: ControlPlane = Depends(get_plane)):
    return _decide(plane, current.session, approval_id, False)


def _decide(plane: ControlPlane, session, approval_id: str,
            approved: bool) -> dict:
    try:
        return plane.decide_collaboration_approval(
            session, approval_id, approved)
    except (TaskNotFound, ApprovalNotFoundError):
        raise HTTPException(status_code=404, detail="Not found") from None
    except ApprovalConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
