"""Autonomy API (A69): consultative levels over the real policy."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import Authed, authed_mutation, get_plane, rate_limit
from forge.api.schemas import AutonomyLevelRequest
from forge.control.control_plane import ControlPlane, InvalidRequest

router = APIRouter()


@router.get("/autonomy")
async def autonomy_report(current: Authed = Depends(authed_mutation),
                          plane: ControlPlane = Depends(get_plane)):
    return plane.autonomy_report(current.session)


@router.post("/autonomy", dependencies=[rate_limit("autonomy")])
async def autonomy_set_level(body: AutonomyLevelRequest,
                             current: Authed = Depends(authed_mutation),
                             plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.autonomy_set_level(current.session, body.level)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
