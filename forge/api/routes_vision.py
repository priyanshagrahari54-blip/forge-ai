"""Vision API (A39): provider-independent image understanding.

Uploads are base64 JSON with hard size bounds; every analyze call
passes the A33 ``Resource.VISION / analyze`` gate (approval round
trips with single-use tokens). The screenshot endpoint returns
policy-filtered action PROPOSALS — nothing is ever executed here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from forge.api.deps import Authed, authed, authed_mutation, get_plane, \
    rate_limit
from forge.api.schemas import VisionAnalyzeRequest
from forge.control.control_plane import (ApprovalConflictError,
                                         ApprovalNotFoundError,
                                         ControlPlane, InvalidRequest,
                                         TaskNotFound)
from forge.vision.base import VisionUnavailable
from forge.vision.pipeline import AVAILABLE_PROVIDERS

router = APIRouter()


@router.get("/vision/capabilities")
async def vision_capabilities(current: Authed = Depends(authed)):
    del current
    return {"providers": list(AVAILABLE_PROVIDERS),
            "simulated_only": True,
            "max_image_bytes": 5_000_000,
            "formats": ["png", "jpeg", "bmp", "gif"]}


@router.post("/vision/analyze", dependencies=[rate_limit("vision")])
async def vision_analyze(body: VisionAnalyzeRequest,
                         current: Authed = Depends(authed_mutation),
                         plane: ControlPlane = Depends(get_plane)):
    from fastapi import HTTPException

    try:
        return plane.vision_analyze(current.session, body.image_b64,
                                    approval_id=body.approval_id)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except VisionUnavailable as exc:
        raise HTTPException(status_code=503,
                            detail=f"VISION_UNAVAILABLE: {exc}") from None


@router.post("/vision/propose", dependencies=[rate_limit("vision")])
async def vision_propose(body: VisionAnalyzeRequest,
                         current: Authed = Depends(authed_mutation),
                         plane: ControlPlane = Depends(get_plane)):
    from fastapi import HTTPException

    try:
        return plane.vision_propose(current.session, body.image_b64)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except VisionUnavailable as exc:
        raise HTTPException(status_code=503,
                            detail=f"VISION_UNAVAILABLE: {exc}") from None


@router.get("/vision/approvals")
async def vision_approvals(current: Authed = Depends(authed),
                           plane: ControlPlane = Depends(get_plane)):
    return {"approvals": plane.list_vision_approvals(current.session)}


@router.post("/vision/approvals/{approval_id}/approve",
             dependencies=[rate_limit("vision")])
async def vision_approve(approval_id: str,
                         current: Authed = Depends(authed_mutation),
                         plane: ControlPlane = Depends(get_plane)):
    return _decide(plane, current.session, approval_id, True)


@router.post("/vision/approvals/{approval_id}/deny",
             dependencies=[rate_limit("vision")])
async def vision_deny(approval_id: str,
                      current: Authed = Depends(authed_mutation),
                      plane: ControlPlane = Depends(get_plane)):
    return _decide(plane, current.session, approval_id, False)


def _decide(plane: ControlPlane, session, approval_id: str,
            approved: bool) -> dict:
    from fastapi import HTTPException

    try:
        return plane.decide_vision_approval(session, approval_id, approved)
    except (TaskNotFound, ApprovalNotFoundError):
        raise HTTPException(status_code=404, detail="Not found") from None
    except ApprovalConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
