"""Performance API (A63): real run timings and bounded aggregates."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import Authed, authed_mutation, get_plane
from forge.control.control_plane import (ControlPlane, InvalidRequest,
                                         TaskNotFound)

router = APIRouter()


@router.get("/performance/summary")
async def performance_summary(limit: int = 200,
                              current: Authed = Depends(authed_mutation),
                              plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.performance_summary(current.session, limit)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/performance/runs/{run_id}")
async def performance_run(run_id: str,
                          current: Authed = Depends(authed_mutation),
                          plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.performance_run(current.session, run_id)
    except TaskNotFound:
        raise HTTPException(status_code=404,
                            detail="Unknown task.") from None
