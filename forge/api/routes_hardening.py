"""Security hardening API (A61): read-only audit report."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from forge.api.deps import Authed, authed_mutation, get_plane
from forge.control.control_plane import ControlPlane

router = APIRouter()


@router.get("/hardening/report")
async def hardening_report(current: Authed = Depends(authed_mutation),
                           plane: ControlPlane = Depends(get_plane)):
    return plane.hardening_report(current.session)
