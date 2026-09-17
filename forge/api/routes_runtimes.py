"""Authenticated runtime verification status (no secrets)."""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from forge.api.deps import Authed, authed, authed_mutation, get_plane, rate_limit
from forge.control.control_plane import ControlPlane
router=APIRouter()
def _monitor(plane:ControlPlane):
    monitor=getattr(plane,"runtime_monitor",None)
    if monitor is None: raise HTTPException(status_code=503,detail="Runtime monitor is not configured.")
    return monitor
@router.get("/runtimes")
async def runtime_status(current:Authed=Depends(authed),plane:ControlPlane=Depends(get_plane)):
    del current; return _monitor(plane).snapshot()
@router.post("/runtimes/check",dependencies=[rate_limit("models")])
async def runtime_check(current:Authed=Depends(authed_mutation),plane:ControlPlane=Depends(get_plane)):
    del current; return _monitor(plane).tick(force=True)
