"""Safe commands, NL interpretation, voice, desktop foundations (A34)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from forge.api.deps import (
    Authed,
    authed,
    authed_mutation,
    get_plane,
    rate_limit,
)
from forge.api.schemas import (
    CommandRequest,
    DesktopCheckRequest,
    InterpretRequest,
    VoiceRequest,
)
from forge.control.control_plane import ControlPlane

router = APIRouter()


@router.post("/commands")
async def execute_command(body: CommandRequest,
                          current: Authed = Depends(authed_mutation),
                          plane: ControlPlane = Depends(get_plane)):
    return plane.execute_command(
        current.session, body.command, task_id=body.task_id,
        approval_id=body.approval_id, requirement=body.requirement,
        expected_version=body.expected_version)


@router.post("/interpret")
async def interpret(body: InterpretRequest,
                    current: Authed = Depends(authed),
                    plane: ControlPlane = Depends(get_plane)):
    return plane.interpret(current.session, body.text)


@router.post("/voice/interpret")
async def voice_interpret(body: VoiceRequest,
                          current: Authed = Depends(authed),
                          plane: ControlPlane = Depends(get_plane)):
    return plane.voice_interpret(current.session, body.text)


@router.get("/desktop/capabilities")
async def desktop_capabilities(_: Authed = Depends(authed),
                               plane: ControlPlane = Depends(get_plane)):
    return plane.desktop_capabilities()


@router.post("/desktop/check")
async def desktop_check(body: DesktopCheckRequest,
                        current: Authed = Depends(authed),
                        plane: ControlPlane = Depends(get_plane)):
    return plane.desktop_check(current.session, body.action,
                               target=body.target)
