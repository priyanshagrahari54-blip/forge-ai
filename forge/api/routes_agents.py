"""Cockpit catalog surfaces (A41) + agent creation (A49).

Endpoints return non-sensitive architecture metadata — never
credentials, secrets, or raw audit content. Agent definitions are
validated specifications; creating one grants no capabilities.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import (Authed, authed, authed_mutation, get_plane,
                            rate_limit)
from forge.api.schemas import (AgentCreateRequest, AgentOutcomeRequest,
                               AgentUpdateRequest)
from forge.control.control_plane import ControlPlane, InvalidRequest

router = APIRouter()


@router.get("/agents")
async def agents(current: Authed = Depends(authed),
                 plane: ControlPlane = Depends(get_plane)):
    del current
    return {"agents": plane.agent_catalog()}


@router.get("/security")
async def security(current: Authed = Depends(authed),
                   plane: ControlPlane = Depends(get_plane)):
    return plane.security_overview(current.session)

# -- A49 agent creation ---------------------------------------------------------------

@router.post("/agents", dependencies=[rate_limit("agents")])
async def create_agent(body: AgentCreateRequest,
                       current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.agent_create(
            current.session, body.name, body.role,
            list(body.capabilities), description=body.description,
            bind=body.bind)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/agents/defined")
async def defined_agents(current: Authed = Depends(authed_mutation),
                         plane: ControlPlane = Depends(get_plane)):
    return plane.agent_definitions(current.session)


@router.patch("/agents/{name}", dependencies=[rate_limit("agents")])
async def update_agent(name: str, body: AgentUpdateRequest,
                       current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.agent_update(
            current.session, name, role=body.role,
            capabilities=list(body.capabilities)
            if body.capabilities is not None else None,
            description=body.description)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.delete("/agents/{name}", dependencies=[rate_limit("agents")])
async def delete_agent(name: str,
                       current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.agent_delete(current.session, name)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

# -- A50 agent evolution ----------------------------------------------------------------

@router.post("/agents/{name}/outcomes", dependencies=[rate_limit("agents")])
async def record_outcome(name: str, body: AgentOutcomeRequest,
                         current: Authed = Depends(authed_mutation),
                         plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.agent_record_outcome(current.session, name,
                                          body.task_id)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/agents/{name}/evolution")
async def agent_evolution(name: str,
                          current: Authed = Depends(authed_mutation),
                          plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.agent_evolution(current.session, name)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
