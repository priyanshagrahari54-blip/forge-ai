"""Read-only milestone projection routes for the Forge cockpit."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from forge.api.deps import Authed, authed, get_plane, rate_limit
from forge.control.control_plane import ControlPlane
from forge.orchestration.task_milestones import project_milestones

router = APIRouter()


@router.get("/tasks/{task_id}/milestones",
            dependencies=[rate_limit("events")])
async def task_milestones(task_id: str,
                          current: Authed = Depends(authed),
                          plane: ControlPlane = Depends(get_plane)):
    """Return milestone progress derived only from persisted task events."""
    task = plane.get_task(current.session, task_id)
    events, latest = plane.get_task_events(
        current.session, task_id, after=0, limit=500)
    projection = project_milestones(events, task_status=str(task.status))
    return {
        "task_id": task_id,
        "milestones": projection["milestones"],
        "summary": {
            key: projection[key]
            for key in ("total", "passed", "running", "failed", "pending")
        },
        "task_status": projection["task_status"],
        "source": projection["source"],
        "observed_events": projection["observed_events"],
        "latest_seq": latest,
    }
