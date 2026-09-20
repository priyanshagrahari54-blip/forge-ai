"""Multimodal specialist and endpoint state (read-only).

Reports what Forge can genuinely serve: which self-hosted endpoints are
configured, which fabric models back them, which of the 100 multimodal
specialists are registered, and — for every modality with no endpoint — the
exact variable that would enable it. Nothing here activates a provider.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from forge.api.deps import Authed, authed, get_plane
from forge.control.control_plane import ControlPlane
from forge.agents.multimodal_fleet import multimodal_readiness

router = APIRouter()


@router.get("/multimodal")
async def multimodal(current: Authed = Depends(authed),
                     plane: ControlPlane = Depends(get_plane)) -> Dict[str, Any]:
    del current
    fabric = getattr(plane, "fabric", None)
    reports = multimodal_readiness(fabric) if fabric is not None else ()
    registration = getattr(plane, "multimodal_report", {}) or {}
    return {
        "schema_version": 1,
        "registered_models": list(registration.get("registered", [])),
        "configured": list(registration.get("available", [])),
        "modalities": [report.to_dict() for report in reports],
        "missing": [report.family for report in reports
                    if not report.registered],
        "note": ("specialists exist for every modality; a specialist is listed "
                 "as registered only when a real model advertises its "
                 "capability"),
    }
