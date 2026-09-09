"""Voice conversation API (A42): multi-turn, interruptible, confirming.

Turns accept text or WAV audio (base64); task creation from voice
still passes the A36 voice permission gate; interruption stops a turn
before any action runs.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import Authed, authed_mutation, get_plane, rate_limit
from forge.api.schemas import VoiceProcessRequest
from forge.control.control_plane import (Conflict, ControlPlane,
                                         InvalidRequest, TaskNotFound)

router = APIRouter()


@router.post("/voice/conversations", dependencies=[rate_limit("voice")])
async def conversation_start(current: Authed = Depends(authed_mutation),
                             plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.voice_conversation_start(current.session)
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/voice/conversations/{conversation_id}/say",
             dependencies=[rate_limit("voice")])
async def conversation_say(conversation_id: str,
                           body: VoiceProcessRequest,
                           current: Authed = Depends(authed_mutation),
                           plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.voice_conversation_say(
            current.session, conversation_id, text=body.text,
            audio_b64=body.audio_b64, approval_id=body.approval_id,
            confirm=body.confirm)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="Not found") from None


@router.post("/voice/conversations/{conversation_id}/interrupt",
             dependencies=[rate_limit("voice")])
async def conversation_interrupt(conversation_id: str,
                                 current: Authed = Depends(authed_mutation),
                                 plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.voice_conversation_interrupt(
            current.session, conversation_id)
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="Not found") from None


@router.get("/voice/conversations/{conversation_id}")
async def conversation_state(conversation_id: str,
                             current: Authed = Depends(authed_mutation),
                             plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.voice_conversation_state(
            current.session, conversation_id)
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="Not found") from None
