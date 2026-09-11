"""First-party Agent Creation Engine endpoints.

Specs become lifecycle-gated packages here; creation grants no
capabilities, and every lifecycle/grant/run decision is audited by the
control plane. Agents created through these endpoints execute only via
the mediated runtime (Model Fabric, PolicyGate, Tool Runtime, namespaced
memory, verification, checkpoints) and can never self-grant permissions.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import (Authed, authed, authed_mutation, get_plane,
                            rate_limit)
from forge.api.schemas import (EngineCreateRequest, EngineGrantRequest,
                               EngineImportRequest, EngineRunRequest,
                               EngineVersionRequest)
from forge.control.control_plane import ControlPlane, InvalidRequest

router = APIRouter()


def _bad(exc: InvalidRequest) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/engine/templates")
async def engine_templates(current: Authed = Depends(authed),
                           plane: ControlPlane = Depends(get_plane)):
    del current, plane
    from forge.agents.creation import AgentCreationEngine

    return {"templates": AgentCreationEngine().templates()}


@router.post("/engine/agents", dependencies=[rate_limit("agents")])
async def engine_create(body: EngineCreateRequest,
                        current: Authed = Depends(authed_mutation),
                        plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.engine_create(
            current.session, template=body.template, name=body.name,
            spec=body.spec, overrides=body.overrides, bind=body.bind)
    except InvalidRequest as exc:
        raise _bad(exc) from None


@router.get("/engine/agents")
async def engine_list(current: Authed = Depends(authed_mutation),
                      plane: ControlPlane = Depends(get_plane)):
    return plane.engine_list(current.session)


@router.post("/engine/agents/import",
             dependencies=[rate_limit("agents")])
async def engine_import(body: EngineImportRequest,
                        current: Authed = Depends(authed_mutation),
                        plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.engine_import(current.session, body.payload)
    except InvalidRequest as exc:
        raise _bad(exc) from None


@router.get("/engine/agents/{name}")
async def engine_get(name: str,
                     current: Authed = Depends(authed_mutation),
                     plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.engine_get(current.session, name)
    except InvalidRequest as exc:
        raise _bad(exc) from None


@router.get("/engine/agents/{name}/export")
async def engine_export(name: str,
                        current: Authed = Depends(authed_mutation),
                        plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.engine_export(current.session, name)
    except InvalidRequest as exc:
        raise _bad(exc) from None


def _lifecycle(operation: str):
    async def endpoint(name: str,
                       current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
        try:
            action = getattr(plane, "engine_%s" % operation)
            return action(current.session, name)
        except InvalidRequest as exc:
            raise _bad(exc) from None

    endpoint.__name__ = "engine_%s" % operation
    return endpoint


for _operation in ("validate", "test", "enable", "pause", "resume",
                   "disable", "retire"):
    router.post("/engine/agents/{name}/%s" % _operation,
                dependencies=[rate_limit("agents")])(_lifecycle(_operation))
del _operation


@router.post("/engine/agents/{name}/grant",
             dependencies=[rate_limit("agents")])
async def engine_grant(name: str, body: EngineGrantRequest,
                       current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.engine_grant(current.session, name, body.index)
    except InvalidRequest as exc:
        raise _bad(exc) from None


@router.post("/engine/agents/{name}/revoke",
             dependencies=[rate_limit("agents")])
async def engine_revoke(name: str, body: EngineGrantRequest,
                        current: Authed = Depends(authed_mutation),
                        plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.engine_revoke(current.session, name, body.index)
    except InvalidRequest as exc:
        raise _bad(exc) from None


@router.post("/engine/agents/{name}/version",
             dependencies=[rate_limit("agents")])
async def engine_version(name: str, body: EngineVersionRequest,
                         current: Authed = Depends(authed_mutation),
                         plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.engine_version(current.session, name,
                                    notes=body.notes, kind=body.kind)
    except InvalidRequest as exc:
        raise _bad(exc) from None


@router.post("/engine/agents/{name}/run",
             dependencies=[rate_limit("agents")])
async def engine_run(name: str, body: EngineRunRequest,
                     current: Authed = Depends(authed_mutation),
                     plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.engine_run(
            current.session, name, body.requirement,
            approval_token_id=body.approval_token_id,
            test_command=body.test_command)
    except InvalidRequest as exc:
        raise _bad(exc) from None
