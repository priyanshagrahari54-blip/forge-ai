"""Cockpit catalog surfaces (A41) + agent creation (A49).

Endpoints return non-sensitive architecture metadata — never
credentials, secrets, or raw audit content. Agent definitions are
validated specifications; creating one grants no capabilities.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import (Authed, authed, authed_mutation, get_plane,
                            rate_limit)
from forge.api.schemas import (AgentCreateRequest, AgentImportRequest,
                               AgentLimitsRequest,
                               AgentMemorySetRequest,
                               AgentOutcomeRequest, AgentRunRequest,
                               AgentStatusRequest, AgentUpdateRequest,
                               EngineAgentCreateRequest,
                               EngineAgentPermissionsRequest,
                               EngineAgentRunRequest,
                               EngineAgentUpdateRequest,
                               SelfDevApplyRequest)
from forge.control.control_plane import (ApprovalConflictError,
                                         ApprovalNotFoundError,
                                         ControlPlane, Forbidden,
                                         InvalidRequest,
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

# -- A53 agent memory -------------------------------------------------------------------

@router.post("/agents/{name}/memory", dependencies=[rate_limit("agents")])
async def set_agent_memory(name: str, body: AgentMemorySetRequest,
                           current: Authed = Depends(authed_mutation),
                           plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.agent_memory_set(current.session, name, body.key,
                                      body.value)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/agents/{name}/memory")
async def list_agent_memory(name: str,
                            current: Authed = Depends(authed_mutation),
                            plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.agent_memory_list(current.session, name)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/agents/{name}/memory/{key}")
async def get_agent_memory(name: str, key: str,
                           current: Authed = Depends(authed_mutation),
                           plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.agent_memory_get(current.session, name, key)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.delete("/agents/{name}/memory/{key}",
               dependencies=[rate_limit("agents")])
async def delete_agent_memory(name: str, key: str,
                              current: Authed = Depends(authed_mutation),
                              plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.agent_memory_delete(current.session, name, key)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

# -- A55 agent lifecycle ----------------------------------------------------------------

@router.post("/agents/{name}/status", dependencies=[rate_limit("agents")])
async def set_agent_status(name: str, body: AgentStatusRequest,
                           current: Authed = Depends(authed_mutation),
                           plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.agent_set_status(current.session, name, body.status)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

# -- A56 agent packaging ----------------------------------------------------------------

@router.get("/agents/{name}/export")
async def export_agent(name: str,
                       current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.agent_export(current.session, name)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/agents/import", dependencies=[rate_limit("agents")])
async def import_agent(body: AgentImportRequest,
                       current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.agent_import(current.session, body.payload)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

# -- A57 agent governance ----------------------------------------------------------------

@router.put("/agents/{name}/limits", dependencies=[rate_limit("agents")])
async def set_agent_limits(name: str, body: AgentLimitsRequest,
                           current: Authed = Depends(authed_mutation),
                           plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.agent_set_limits(
            current.session, name,
            max_runs_per_hour=body.max_runs_per_hour,
            max_concurrent=body.max_concurrent)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/agents/{name}/limits")
async def get_agent_limits(name: str,
                           current: Authed = Depends(authed_mutation),
                           plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.agent_limits(current.session, name)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

# -- A58 agent self-development ----------------------------------------------------------

@router.post("/agents/{name}/selfdev/analyze",
             dependencies=[rate_limit("agents")])
async def selfdev_analyze(name: str,
                          current: Authed = Depends(authed_mutation),
                          plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.selfdev_analyze(current.session, name)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/agents/{name}/selfdev/apply",
             dependencies=[rate_limit("agents")])
async def selfdev_apply(name: str, body: SelfDevApplyRequest,
                        current: Authed = Depends(authed_mutation),
                        plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.selfdev_apply(current.session, name,
                                   body.proposal_id)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/agents/{name}/selfdev")
async def selfdev_ledger(name: str,
                         current: Authed = Depends(authed_mutation),
                         plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.selfdev_ledger(current.session, name)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

# -- Agent Creation Engine (first-party) ----------------------------------------------------------

def _engine_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, Forbidden):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, InvalidRequest):
        return HTTPException(status_code=400, detail=str(exc))
    raise exc


@router.get("/agent-specs/templates")
async def engine_templates(current: Authed = Depends(authed_mutation),
                           plane: ControlPlane = Depends(get_plane)):
    return plane.spec_agent_templates(current.session)


@router.post("/agent-specs", dependencies=[rate_limit("agents")])
async def engine_create(body: EngineAgentCreateRequest,
                        current: Authed = Depends(authed_mutation),
                        plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.spec_agent_create(
            current.session, body.spec, bind=body.bind)
    except (InvalidRequest, Forbidden) as exc:
        raise _engine_errors(exc) from None


@router.get("/agent-specs")
async def engine_list(current: Authed = Depends(authed_mutation),
                      plane: ControlPlane = Depends(get_plane)):
    return plane.spec_agent_list(current.session)


@router.get("/agent-specs/{name}")
async def engine_get(name: str,
                     current: Authed = Depends(authed_mutation),
                     plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.spec_agent_get(current.session, name)
    except (InvalidRequest, Forbidden) as exc:
        raise _engine_errors(exc) from None


@router.put("/agent-specs/{name}", dependencies=[rate_limit("agents")])
async def engine_update(name: str, body: EngineAgentUpdateRequest,
                        current: Authed = Depends(authed_mutation),
                        plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.spec_agent_update(
            current.session, name, body.spec)
    except (InvalidRequest, Forbidden) as exc:
        raise _engine_errors(exc) from None


@router.delete("/agent-specs/{name}",
               dependencies=[rate_limit("agents")])
async def engine_delete(name: str,
                        current: Authed = Depends(authed_mutation),
                        plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.spec_agent_delete(current.session, name)
    except (InvalidRequest, Forbidden) as exc:
        raise _engine_errors(exc) from None


@router.post("/agent-specs/{name}/validate",
             dependencies=[rate_limit("agents")])
async def engine_validate(name: str,
                          current: Authed = Depends(authed_mutation),
                          plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.spec_agent_validate(current.session, name)
    except (InvalidRequest, Forbidden) as exc:
        raise _engine_errors(exc) from None


@router.post("/agent-specs/{name}/test",
             dependencies=[rate_limit("agents")])
async def engine_test(name: str,
                      current: Authed = Depends(authed_mutation),
                      plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.spec_agent_test(current.session, name)
    except (InvalidRequest, Forbidden) as exc:
        raise _engine_errors(exc) from None


@router.post("/agent-specs/{name}/enable",
             dependencies=[rate_limit("agents")])
async def engine_enable(name: str,
                        current: Authed = Depends(authed_mutation),
                        plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.spec_agent_enable(current.session, name)
    except (InvalidRequest, Forbidden) as exc:
        raise _engine_errors(exc) from None


@router.post("/agent-specs/{name}/pause",
             dependencies=[rate_limit("agents")])
async def engine_pause(name: str,
                       current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.spec_agent_pause(current.session, name)
    except (InvalidRequest, Forbidden) as exc:
        raise _engine_errors(exc) from None


@router.post("/agent-specs/{name}/disable",
             dependencies=[rate_limit("agents")])
async def engine_disable(name: str,
                         current: Authed = Depends(authed_mutation),
                         plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.spec_agent_disable(current.session, name)
    except (InvalidRequest, Forbidden) as exc:
        raise _engine_errors(exc) from None


@router.post("/agent-specs/{name}/retire",
             dependencies=[rate_limit("agents")])
async def engine_retire(name: str,
                        current: Authed = Depends(authed_mutation),
                        plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.spec_agent_retire(current.session, name)
    except (InvalidRequest, Forbidden) as exc:
        raise _engine_errors(exc) from None


@router.put("/agent-specs/{name}/permissions",
            dependencies=[rate_limit("agents")])
async def engine_permissions(name: str,
                             body: EngineAgentPermissionsRequest,
                             current: Authed = Depends(authed_mutation),
                             plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.spec_agent_set_permissions(
            current.session, name, list(body.permissions))
    except (InvalidRequest, Forbidden) as exc:
        raise _engine_errors(exc) from None


@router.get("/agent-specs/{name}/versions")
async def engine_versions(name: str,
                          current: Authed = Depends(authed_mutation),
                          plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.spec_agent_versions(current.session, name)
    except (InvalidRequest, Forbidden) as exc:
        raise _engine_errors(exc) from None


@router.post("/agent-specs/{name}/run",
             dependencies=[rate_limit("agents")])
async def engine_run(name: str, body: EngineAgentRunRequest,
                     current: Authed = Depends(authed_mutation),
                     plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.spec_agent_run(
            current.session, name, body.requirement,
            approval_id=body.approval_id)
    except (InvalidRequest, Forbidden) as exc:
        raise _engine_errors(exc) from None


@router.get("/agent-specs/{name}/runs/{run_id}")
async def engine_run_result(name: str, run_id: str,
                            current: Authed = Depends(authed_mutation),
                            plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.spec_agent_run_result(
            current.session, name, run_id)
    except (InvalidRequest, Forbidden) as exc:
        raise _engine_errors(exc) from None


@router.get("/agent-specs/{name}/runs")
async def engine_runs(name: str,
                      current: Authed = Depends(authed_mutation),
                      plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.spec_agent_runs(current.session, name)
    except (InvalidRequest, Forbidden) as exc:
        raise _engine_errors(exc) from None
