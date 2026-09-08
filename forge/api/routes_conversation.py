"""General conversation API (A43): classify → route → real answers or
real tasks."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import Authed, authed_mutation, get_plane, rate_limit
from forge.api.schemas import ConversationRequest
from forge.control.control_plane import ControlPlane

router = APIRouter()


@router.post("/conversation", dependencies=[rate_limit("conversation")])
async def converse(body: ConversationRequest,
                   current: Authed = Depends(authed_mutation),
                   plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.converse(current.session, body.message)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/conversation")
async def conversation_history(current: Authed = Depends(authed_mutation),
                               plane: ControlPlane = Depends(get_plane)):
    return plane.conversation_history(current.session)
