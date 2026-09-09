"""Model Fabric API (A46): gated fabric generation via the plane."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import Authed, authed_mutation, get_plane, rate_limit
from forge.api.schemas import ModelGenerateRequest
from forge.control.control_plane import (ApprovalConflictError,
                                         ApprovalNotFoundError,
                                         ControlPlane, InvalidRequest,
                                         TaskNotFound)

router = APIRouter()


@router.post("/models/generate", dependencies=[rate_limit("models")])
async def generate(body: ModelGenerateRequest,
                   current: Authed = Depends(authed_mutation),
                   plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.model_generate(
            current.session, body.prompt, capability=body.capability,
            approval_id=body.approval_id)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/models/approvals")
async def approvals(current: Authed = Depends(authed_mutation),
                    plane: ControlPlane = Depends(get_plane)):
    return {"approvals": plane.list_model_approvals(current.session)}


@router.post("/models/approvals/{approval_id}/approve",
             dependencies=[rate_limit("models")])
async def approve(approval_id: str,
                  current: Authed = Depends(authed_mutation),
                  plane: ControlPlane = Depends(get_plane)):
    return _decide(plane, current.session, approval_id, True)


@router.post("/models/approvals/{approval_id}/deny",
             dependencies=[rate_limit("models")])
async def deny(approval_id: str,
               current: Authed = Depends(authed_mutation),
               plane: ControlPlane = Depends(get_plane)):
    return _decide(plane, current.session, approval_id, False)


def _decide(plane: ControlPlane, session, approval_id: str,
            approved: bool) -> dict:
    try:
        return plane.decide_model_approval(session, approval_id, approved)
    except (TaskNotFound, ApprovalNotFoundError):
        raise HTTPException(status_code=404, detail="Not found") from None
    except ApprovalConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
