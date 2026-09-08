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
                               AgentRunRequest, AgentUpdateRequest)
from forge.control.control_plane import (ApprovalConflictError,
                                         ApprovalNotFoundError,
                                         ControlPlane, InvalidRequest,
                                         TaskNotFound)

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

# -- A51 agent execution ----------------------------------------------------------------

@router.post("/agents/{name}/run", dependencies=[rate_limit("agents")])
async def run_agent(name: str, body: AgentRunRequest,
                    current: Authed = Depends(authed_mutation),
                    plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.agent_run(current.session, name, body.requirement,
                               approval_id=body.approval_id)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/agents/{name}/runs")
async def agent_runs(name: str,
                     current: Authed = Depends(authed_mutation),
                     plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.agent_runs(current.session, name)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/agents/runs/approvals")
async def agent_run_approvals(current: Authed = Depends(authed_mutation),
                              plane: ControlPlane = Depends(get_plane)):
    return {"approvals": plane.list_agent_run_approvals(current.session)}


@router.post("/agents/runs/approvals/{approval_id}/approve",
             dependencies=[rate_limit("agents")])
async def approve_agent_run(approval_id: str,
                            current: Authed = Depends(authed_mutation),
                            plane: ControlPlane = Depends(get_plane)):
    return _decide_agent_run(plane, current.session, approval_id, True)


@router.post("/agents/runs/approvals/{approval_id}/deny",
             dependencies=[rate_limit("agents")])
async def deny_agent_run(approval_id: str,
                         current: Authed = Depends(authed_mutation),
                         plane: ControlPlane = Depends(get_plane)):
    return _decide_agent_run(plane, current.session, approval_id, False)


def _decide_agent_run(plane: ControlPlane, session, approval_id: str,
                      approved: bool) -> dict:
    try:
        return plane.decide_agent_run_approval(
            session, approval_id, approved)
    except (TaskNotFound, ApprovalNotFoundError):
        raise HTTPException(status_code=404, detail="Not found") from None
    except ApprovalConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

@router.get("/agents/{name}/runs/{run_id}")
async def agent_run_result(name: str, run_id: str,
                           current: Authed = Depends(authed_mutation),
                           plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.agent_run_result(current.session, name, run_id)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
