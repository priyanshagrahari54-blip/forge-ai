"""Authenticated remote-worker control-plane endpoints."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from forge.api.deps import Authed, authed, authed_mutation, get_plane
from forge.control.control_plane import ControlPlane
from forge.workers.registry import WorkerRegistry

router = APIRouter()


def _registry(plane: ControlPlane) -> WorkerRegistry:
    registry = getattr(plane, "worker_registry", None)
    if registry is None:
        registry = WorkerRegistry()
        plane.worker_registry = registry
    return registry


class WorkerRegistration(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    capabilities: list[str] = Field(default_factory=list)
    cpu_threads: int = Field(default=1, ge=1)
    ram_mb: int = Field(default=0, ge=0)
    gpu: bool = False
    platform: str = Field(default="unknown", max_length=100)
    endpoint: str = Field(default="", max_length=500)


class WorkerHeartbeat(BaseModel):
    worker_id: str = Field(min_length=1, max_length=100)


@router.get("/workers")
async def workers(current: Authed = Depends(authed), plane: ControlPlane = Depends(get_plane)) -> Dict[str, Any]:
    del current
    return _registry(plane).snapshot()


@router.post("/workers/register")
async def register_worker(body: WorkerRegistration, current: Authed = Depends(authed_mutation), plane: ControlPlane = Depends(get_plane)) -> Dict[str, Any]:
    del current
    try:
        worker = _registry(plane).register(
            name=body.name,
            capabilities=tuple(body.capabilities),
            cpu_threads=body.cpu_threads,
            ram_mb=body.ram_mb,
            gpu=body.gpu,
            platform=body.platform,
            endpoint=body.endpoint,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return worker.to_dict(ttl=_registry(plane).heartbeat_ttl)


@router.post("/workers/heartbeat")
async def heartbeat_worker(body: WorkerHeartbeat, current: Authed = Depends(authed_mutation), plane: ControlPlane = Depends(get_plane)) -> Dict[str, Any]:
    del current
    if not _registry(plane).heartbeat(body.worker_id):
        raise HTTPException(status_code=404, detail="Worker is unknown or revoked.")
    return {"ok": True, "worker_id": body.worker_id}


@router.post("/workers/{worker_id}/revoke")
async def revoke_worker(worker_id: str, current: Authed = Depends(authed_mutation), plane: ControlPlane = Depends(get_plane)) -> Dict[str, Any]:
    del current
    if not _registry(plane).revoke(worker_id):
        raise HTTPException(status_code=404, detail="Worker not found.")
    return {"ok": True, "worker_id": worker_id, "revoked": True}
