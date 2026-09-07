"""Read-only model and permission views (A34)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from forge.api.deps import Authed, authed, get_plane
from forge.control.control_plane import ControlPlane

router = APIRouter()


@router.get("/models")
async def list_models(_: Authed = Depends(authed),
                      plane: ControlPlane = Depends(get_plane)):
    state = plane.get_model_state()
    return {"models": state["models"],
            "routing_policy": state["routing_policy"],
            "recent_routing": state["recent_routing"]}


@router.get("/models/health")
async def models_health(_: Authed = Depends(authed),
                        plane: ControlPlane = Depends(get_plane)):
    return plane.model_health()


@router.get("/providers")
async def list_providers(_: Authed = Depends(authed),
                         plane: ControlPlane = Depends(get_plane)):
    state = plane.get_model_state()
    return {"providers": state["providers"],
            "health": state["provider_health"]}


@router.get("/permissions")
async def permissions(current: Authed = Depends(authed),
                      plane: ControlPlane = Depends(get_plane)):
    return plane.get_permission_state(current.session)
