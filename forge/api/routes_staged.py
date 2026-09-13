"""Staged-build routes: project sections with ordered stages (A82)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

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
    StagedPreviewUpdateRequest,
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


# -- live preview ----------------------------------------------------------


@router.get("/builds/{build_id}/preview")
async def get_preview(build_id: str,
                      current: Authed = Depends(authed),
                      plane: ControlPlane = Depends(get_plane)):
    return _service(plane).get_preview(current.session, build_id)


@router.patch("/builds/{build_id}/preview")
async def set_preview_entry(build_id: str,
                            body: StagedPreviewUpdateRequest,
                            current: Authed = Depends(authed_mutation),
                            plane: ControlPlane = Depends(get_plane)):
    _service(plane).set_preview_entry(
        current.session, build_id, body.entry)
    return _service(plane).get_preview(current.session, build_id)


@router.get("/builds/{build_id}/preview/file")
async def preview_file(build_id: str, path: str = "",
                       current: Authed = Depends(authed),
                       plane: ControlPlane = Depends(get_plane)):
    from urllib.parse import quote

    payload = _service(plane).read_preview_file(
        current.session, build_id, path)
    if payload.get("kind") == "image":
        payload["raw_url"] = (
            "/api/v1/builds/%s/preview/raw?path=%s"
            % (build_id, quote(payload["path"], safe="")))
    return payload


@router.get("/builds/{build_id}/preview/raw")
async def preview_raw(build_id: str, path: str = "",
                      current: Authed = Depends(authed),
                      plane: ControlPlane = Depends(get_plane)):
    abspath, media_type = _service(plane).resolve_preview_raw(
        current.session, build_id, path)
    # The cockpit embeds this in a sandboxed iframe (no
    # allow-same-origin), and the document carries its own sandbox CSP:
    # previewed scripts can never reach the cockpit DOM, cookies, or
    # storage. no-store keeps every refresh live.
    return FileResponse(
        abspath, media_type=media_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox allow-scripts",
            "Cache-Control": "no-store",
        })
