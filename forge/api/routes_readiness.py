"""Authenticated control-plane readiness reporting."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from forge.api.deps import Authed, authed, get_plane
from forge.control.control_plane import ControlPlane
from forge.diagnostics import build_readiness

router = APIRouter()


@router.get("/readiness")
async def readiness(
    current: Authed = Depends(authed),
    plane: ControlPlane = Depends(get_plane),
) -> Dict[str, Any]:
    del current
    health = None
    monitor = getattr(plane, "runtime_monitor", None)
    if monitor is not None:
        snapshot = getattr(monitor, "snapshot", None)
        if callable(snapshot):
            try:
                payload = snapshot()
                if isinstance(payload, dict):
                    health = payload.get("providers") or payload
            except Exception:
                health = None
    return build_readiness(
        provider_health=health,
        worker_registry=getattr(plane, "worker_registry", None),
    )
