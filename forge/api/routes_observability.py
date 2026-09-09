"""Observability API (A62): plane metrics snapshot."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from forge.api.deps import Authed, authed_mutation, get_plane
from forge.control.control_plane import ControlPlane

router = APIRouter()


@router.get("/observability/metrics")
async def metrics(current: Authed = Depends(authed_mutation),
                  plane: ControlPlane = Depends(get_plane)):
    return plane.observability_snapshot(current.session)
