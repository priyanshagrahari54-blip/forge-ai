"""Outbound channel status (read-only).

Sending is an action and stays behind the existing approval path — a voice
intent that is approved delivers through `ControlPlane._voice_task_factory`.
This endpoint reports only what is configured, so a UI can be honest about
which channels exist and what they need.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from forge.api.deps import Authed, authed, get_plane
from forge.control.control_plane import ControlPlane

router = APIRouter()


@router.get("/channels")
async def channels(current: Authed = Depends(authed),
                   plane: ControlPlane = Depends(get_plane)):
    del current
    return plane.channels().status()
