"""Staged-build routes: project sections with ordered stages (A82)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends

from forge.api.deps import (
    Authed,
    authed,
    authed_mutation,
    get_plane,
    rate_limit,
)
from forge.api.schemas import (
    StagedBuildCreateRequest,
    StagedBuildUpdateRequest,
    StagedRunRequest,
    StagedStageUpdateRequest,
    StagedStagesAddRequest,
)
from forge.control.control_plane import ControlPlane
from forge.staged.service import StagedBuilds

router = APIRouter()


def _service(plane: ControlPlane) -> StagedBuilds:
    return StagedBuilds(plane)


@router.get("/builds")
async def list_builds(current: Authed = Depends(authed),
                      plane: ControlPlane = Depends(get_plane)):
    builds = _service(plane).list_builds(current.session)
    return {"builds": builds}


@router.post("/builds", dependencies=[rate_limit("task_create")])
async def create_build(body: StagedBuildCreateRequest,
                       current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    build = _service(plane).create_build(
        current.session, body.name, description=body.description,
        roadmap=body.roadmap, blueprint=body.blueprint)
    return {"build": build.to_dict(include_docs=True)}


@router.get("/builds/{build_id}")
async def get_build(build_id: str,
                    current: Authed = Depends(authed),
                    plane: ControlPlane = Depends(get_plane)):
    return _service(plane).get_board(current.session, build_id)


@router.patch("/builds/{build_id}")
async def update_build(build_id: str, body: StagedBuildUpdateRequest,
                       current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    build = _service(plane).update_build(
        current.session, build_id, name=body.name,
        description=body.description, roadmap=body.roadmap,
        blueprint=body.blueprint)
    return {"build": build.to_dict(include_docs=True)}


@router.delete("/builds/{build_id}")
async def delete_build(build_id: str,
                       current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    return _service(plane).delete_build(current.session, build_id)


@router.post("/builds/{build_id}/stages",
             dependencies=[rate_limit("task_create")])
async def add_stages(build_id: str, body: StagedStagesAddRequest,
                     current: Authed = Depends(authed_mutation),
                     plane: ControlPlane = Depends(get_plane)):
    stages = _service(plane).add_stages(
        current.session, build_id,
        [item.model_dump() for item in body.stages])
    return {"stages": [stage.to_dict() for stage in stages]}


@router.patch("/builds/{build_id}/stages/{position}")
async def update_stage(build_id: str, position: int,
                       body: StagedStageUpdateRequest,
                       current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    stage = _service(plane).update_stage(
        current.session, build_id, position,
        title=body.title, prompt=body.prompt)
    return {"stage": stage.to_dict()}


@router.delete("/builds/{build_id}/stages/{position}")
async def delete_stage(build_id: str, position: int,
                       current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    return _service(plane).delete_stage(
        current.session, build_id, position)


@router.post("/builds/{build_id}/run-next",
             dependencies=[rate_limit("task_create")])
async def run_next(build_id: str, body: Optional[StagedRunRequest] = None,
                   current: Authed = Depends(authed_mutation),
                   plane: ControlPlane = Depends(get_plane)):
    return _service(plane).run_next(
        current.session, build_id, mode=body.mode if body else "")


@router.post("/builds/{build_id}/stages/{position}/run",
             dependencies=[rate_limit("task_create")])
async def run_stage(build_id: str, position: int,
                    body: Optional[StagedRunRequest] = None,
                    current: Authed = Depends(authed_mutation),
                    plane: ControlPlane = Depends(get_plane)):
    return _service(plane).run_stage(
        current.session, build_id, position,
        mode=body.mode if body else "")


@router.get("/builds/{build_id}/stages/{position}/evidence")
async def stage_evidence(build_id: str, position: int,
                         current: Authed = Depends(authed),
                         plane: ControlPlane = Depends(get_plane)):
    return _service(plane).stage_evidence(
        current.session, build_id, position)
