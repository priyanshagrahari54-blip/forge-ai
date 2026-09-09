"""Model benchmarking API (A60): honest check-based benchmarks."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import Authed, authed_mutation, get_plane, rate_limit
from forge.api.schemas import BenchmarkRequest
from forge.control.control_plane import ControlPlane, InvalidRequest

router = APIRouter()


@router.post("/benchmarks", dependencies=[rate_limit("benchmark")])
async def run_benchmarks(body: BenchmarkRequest,
                         current: Authed = Depends(authed_mutation),
                         plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.benchmark_run(current.session, body.models)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/benchmarks")
async def benchmark_history(limit: int = 20,
                           current: Authed = Depends(authed_mutation),
                           plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.benchmark_history(current.session, limit)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
