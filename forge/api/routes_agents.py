"""Cockpit catalog surfaces (A41): agent inventory + security posture.

Both endpoints return non-sensitive architecture metadata — never
credentials, secrets, or raw audit content.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from forge.api.deps import Authed, authed, get_plane
from forge.control.control_plane import ControlPlane

router = APIRouter()


@router.get("/agents")
async def agents(current: Authed = Depends(authed),
                 plane: ControlPlane = Depends(get_plane)):
    del current
    return {"agents": plane.agent_catalog()}


@router.get("/security")
async def security(current: Authed = Depends(authed),
                   plane: ControlPlane = Depends(get_plane)):
    return plane.security_overview(current.session)
