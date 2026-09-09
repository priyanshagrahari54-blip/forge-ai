"""Plugin SDK API (A66): validated declarative plugins."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import Authed, authed_mutation, get_plane, rate_limit
from forge.api.schemas import PluginInstallRequest
from forge.control.control_plane import ControlPlane, InvalidRequest

router = APIRouter()


@router.post("/plugins", dependencies=[rate_limit("plugins")])
async def install_plugin(body: PluginInstallRequest,
                         current: Authed = Depends(authed_mutation),
                         plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.plugin_install(current.session, body.manifest)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/plugins")
async def list_plugins(current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    return plane.plugin_list(current.session)


@router.get("/plugins/{plugin_id}")
async def plugin_status(plugin_id: str,
                        current: Authed = Depends(authed_mutation),
                        plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.plugin_status(current.session, plugin_id)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.delete("/plugins/{plugin_id}", dependencies=[rate_limit("plugins")])
async def remove_plugin(plugin_id: str,
                        current: Authed = Depends(authed_mutation),
                        plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.plugin_remove(current.session, plugin_id)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
