"""AI Council API (A45): advisory multi-model deliberation."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import Authed, authed_mutation, get_plane, rate_limit
from forge.api.schemas import CouncilQuestionRequest
from forge.control.control_plane import ControlPlane, InvalidRequest

router = APIRouter()


@router.get("/council/capabilities")
async def capabilities(current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    return plane.council_capabilities(current.session)


@router.post("/council/convene", dependencies=[rate_limit("council")])
async def convene(body: CouncilQuestionRequest,
                  current: Authed = Depends(authed_mutation),
                  plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.council_convene(current.session, body.question)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/council/history")
async def history(current: Authed = Depends(authed_mutation),
                  plane: ControlPlane = Depends(get_plane)):
    return plane.council_history(current.session)
