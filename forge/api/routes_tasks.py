"""Task, run, event, report, and checkpoint routes (A34)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends

from forge.api.deps import (
    Authed,
    authed,
    authed_mutation,
    get_plane,
    pagination_params,
    rate_limit,
)
from forge.api.schemas import (
    CreateTaskRequest,
    RollbackRequest,
    TaskActionRequest,
)
from forge.control.control_plane import ControlPlane

router = APIRouter()


@router.get("/tasks")
async def list_tasks(status: str = "",
                     paging: dict = Depends(pagination_params),
                     current: Authed = Depends(authed),
                     plane: ControlPlane = Depends(get_plane)):
    runs, total = plane.list_tasks(
        current.session, status=status, limit=paging["limit"],
        offset=paging["offset"])
    return {"tasks": [run.to_dict() for run in runs], "total": total}


@router.post("/tasks", dependencies=[rate_limit("task_create")])
async def create_task(body: CreateTaskRequest,
                      current: Authed = Depends(authed_mutation),
                      plane: ControlPlane = Depends(get_plane)):
    run = plane.submit_task(current.session, body.requirement,
                            mode=body.mode or "")
    return {"task": run.to_dict()}


@router.get("/tasks/{task_id}")
async def get_task(task_id: str,
                   current: Authed = Depends(authed),
                   plane: ControlPlane = Depends(get_plane)):
    return {"task": plane.get_task(current.session, task_id).to_dict()}


@router.post("/tasks/{task_id}/pause")
async def pause_task(task_id: str, body: Optional[TaskActionRequest] = None,
                     current: Authed = Depends(authed_mutation),
                     plane: ControlPlane = Depends(get_plane)):
    run = plane.pause_task(
        current.session, task_id,
        expected_version=body.expected_version if body else None)
    return {"task": run.to_dict()}


@router.post("/tasks/{task_id}/resume")
async def resume_task(task_id: str, body: Optional[TaskActionRequest] = None,
                      current: Authed = Depends(authed_mutation),
                      plane: ControlPlane = Depends(get_plane)):
    run = plane.resume_task(
        current.session, task_id,
        expected_version=body.expected_version if body else None)
    return {"task": run.to_dict()}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, body: Optional[TaskActionRequest] = None,
                      current: Authed = Depends(authed_mutation),
                      plane: ControlPlane = Depends(get_plane)):
    run = plane.cancel_task(
        current.session, task_id,
        expected_version=body.expected_version if body else None)
    return {"task": run.to_dict()}


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str,
                     current: Authed = Depends(authed_mutation),
                     plane: ControlPlane = Depends(get_plane)):
    run = plane.retry_task(current.session, task_id)
    return {"task": run.to_dict()}


@router.get("/tasks/{task_id}/events",
            dependencies=[rate_limit("events")])
async def task_events(task_id: str, after: int = 0, limit: int = 200,
                      current: Authed = Depends(authed),
                      plane: ControlPlane = Depends(get_plane)):
    events, latest = plane.get_task_events(
        current.session, task_id, after=max(0, after),
        limit=max(1, min(500, limit)))
    return {"events": events, "latest": latest}


@router.get("/tasks/{task_id}/report")
async def task_report(task_id: str,
                      current: Authed = Depends(authed),
                      plane: ControlPlane = Depends(get_plane)):
    return plane.get_task_report(current.session, task_id)


@router.get("/tasks/{task_id}/logs")
async def task_logs(task_id: str,
                    current: Authed = Depends(authed),
                    plane: ControlPlane = Depends(get_plane)):
    return plane.get_task_logs(current.session, task_id)


@router.get("/tasks/{task_id}/verification")
async def task_verification(task_id: str,
                            current: Authed = Depends(authed),
                            plane: ControlPlane = Depends(get_plane)):
    return plane.get_verification(current.session, task_id)


@router.get("/tasks/{task_id}/checkpoints")
async def task_checkpoints(task_id: str,
                           current: Authed = Depends(authed),
                           plane: ControlPlane = Depends(get_plane)):
    return {"checkpoints": plane.list_checkpoints(
        current.session, task_id)}


@router.post("/tasks/{task_id}/rollback")
async def task_rollback(task_id: str, body: Optional[RollbackRequest] = None,
                        current: Authed = Depends(authed_mutation),
                        plane: ControlPlane = Depends(get_plane)):
    result = plane.rollback_task(
        current.session, task_id,
        checkpoint_id=body.checkpoint_id if body else "",
        expected_version=body.expected_version if body else None)
    return result


# -- runs: read aliases over the same run records -------------------------------


@router.get("/runs")
async def list_runs(status: str = "",
                    paging: dict = Depends(pagination_params),
                    current: Authed = Depends(authed),
                    plane: ControlPlane = Depends(get_plane)):
    runs, total = plane.list_tasks(
        current.session, status=status, limit=paging["limit"],
        offset=paging["offset"])
    return {"runs": [run.to_dict() for run in runs], "total": total}


@router.get("/runs/{run_id}")
async def get_run(run_id: str,
                  current: Authed = Depends(authed),
                  plane: ControlPlane = Depends(get_plane)):
    run = plane.get_task(current.session, run_id)
    return {"run": run.to_dict(include_report=True)}
