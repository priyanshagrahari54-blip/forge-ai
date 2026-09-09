"""Failure learning API (A59): persistent bounded failure ledger."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import Authed, authed_mutation, get_plane, rate_limit
from forge.api.schemas import FailureRecordRequest
from forge.control.control_plane import ControlPlane, InvalidRequest

router = APIRouter()


@router.post("/learning/failures", dependencies=[rate_limit("learning")])
async def record_failure(body: FailureRecordRequest,
                         current: Authed = Depends(authed_mutation),
                         plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.record_failure(current.session, body.category,
                                    body.error)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/learning/lessons")
async def failure_lessons(limit: int = 5,
                          current: Authed = Depends(authed_mutation),
                          plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.failure_lessons(current.session, limit)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
