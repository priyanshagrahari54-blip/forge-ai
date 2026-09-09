"""Final acceptance arc API (A71-A80): loops and go/no-go gates."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import Authed, authed_mutation, get_plane, rate_limit
from forge.api.schemas import FinalVerifyRunRequest
from forge.control.control_plane import ControlPlane, InvalidRequest

router = APIRouter()


@router.post("/final/acceptance", dependencies=[rate_limit("final")])
async def final_acceptance(current: Authed = Depends(authed_mutation),
                           plane: ControlPlane = Depends(get_plane)):
    return plane.final_acceptance(current.session)


@router.post("/final/verify-run", dependencies=[rate_limit("final")])
async def final_verify_run(body: FinalVerifyRunRequest,
                           current: Authed = Depends(authed_mutation),
                           plane: ControlPlane = Depends(get_plane)):
    return plane.final_verify_run(current.session, body.run_id)
