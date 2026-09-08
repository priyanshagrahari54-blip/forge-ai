"""Safe commands, NL interpretation, voice, and desktop (A34/A35)."""
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
    DesktopActRequest,
    DesktopCheckRequest,
    DesktopGrantRequest,
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


# -- desktop (A35) ---------------------------------------------------------------


@router.get("/desktop/capabilities")
async def desktop_capabilities(current: Authed = Depends(authed),
                               plane: ControlPlane = Depends(get_plane)):
    return plane.desktop_capabilities(current.session)


@router.post("/desktop/check")
async def desktop_check(body: DesktopCheckRequest,
                        current: Authed = Depends(authed),
                        plane: ControlPlane = Depends(get_plane)):
    return plane.desktop_check(current.session, body.action,
                               target=body.target, params=body.params,
                               task_id=body.task_id)


@router.get("/desktop/state")
async def desktop_state(current: Authed = Depends(authed),
                        plane: ControlPlane = Depends(get_plane)):
    return plane.desktop_state(current.session)


@router.post("/desktop/act",
             dependencies=[rate_limit("desktop")])
async def desktop_act(body: DesktopActRequest,
                      current: Authed = Depends(authed_mutation),
                      plane: ControlPlane = Depends(get_plane)):
    return plane.desktop_act(
        current.session, body.action, target=body.target,
        params=body.params, reason=body.reason, task_id=body.task_id,
        approval_id=body.approval_id)


@router.post("/desktop/grants",
             dependencies=[rate_limit("desktop")])
async def desktop_grant(body: DesktopGrantRequest,
                        current: Authed = Depends(authed_mutation),
                        plane: ControlPlane = Depends(get_plane)):
    return plane.desktop_grant(current.session, body.task_id,
                               body.scopes)


@router.get("/desktop/approvals")
async def desktop_approvals(current: Authed = Depends(authed),
                            plane: ControlPlane = Depends(get_plane)):
    return {"approvals": plane.list_desktop_approvals(current.session)}


@router.post("/desktop/approvals/{approval_id}/approve",
             dependencies=[rate_limit("desktop")])
async def desktop_approve(approval_id: str,
                          current: Authed = Depends(authed_mutation),
                          plane: ControlPlane = Depends(get_plane)):
    return plane.decide_desktop_request(current.session, approval_id, True)


@router.post("/desktop/approvals/{approval_id}/deny",
             dependencies=[rate_limit("desktop")])
async def desktop_deny(approval_id: str,
                       current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    return plane.decide_desktop_request(current.session, approval_id, False)
