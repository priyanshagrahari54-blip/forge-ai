"""Final acceptance arc API (A71-A80): loops and go/no-go gates."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import Authed, authed_mutation, get_plane, rate_limit
from forge.api.schemas import (FinalBenchmarkRequest,
                               FinalGateVerifyRequest,
                               FinalLoopRequest,
                               FinalVerifyRunRequest)
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


@router.post("/final/security", dependencies=[rate_limit("final")])
async def final_security(current: Authed = Depends(authed_mutation),
                         plane: ControlPlane = Depends(get_plane)):
    return plane.final_security_gate(current.session)

@router.post("/final/benchmark", dependencies=[rate_limit("final")])
async def final_benchmark(body: FinalBenchmarkRequest,
                          current: Authed = Depends(authed_mutation),
                          plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.final_benchmark_gate(
            current.session, body.min_passed)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/final/commit", dependencies=[rate_limit("final")])
async def final_commit(current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    return plane.final_commit_gate(current.session)


@router.post("/final/memory", dependencies=[rate_limit("final")])
async def final_memory(current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    return plane.final_memory_gate(current.session)


@router.post("/final/self-evaluation", dependencies=[rate_limit("final")])
async def final_self_evaluation(
        current: Authed = Depends(authed_mutation),
        plane: ControlPlane = Depends(get_plane)):
    return plane.final_self_evaluation(current.session)


@router.post("/final/rollout", dependencies=[rate_limit("final")])
async def final_rollout(current: Authed = Depends(authed_mutation),
                        plane: ControlPlane = Depends(get_plane)):
    return plane.final_rollout(current.session)


@router.post("/final/loop", dependencies=[rate_limit("final")])
async def final_loop(body: FinalLoopRequest,
                     current: Authed = Depends(authed_mutation),
                     plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.final_loop(current.session, body.max_iterations)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/final/gate", dependencies=[rate_limit("final")])
async def final_gate(current: Authed = Depends(authed_mutation),
                     plane: ControlPlane = Depends(get_plane)):
    return plane.final_gate(current.session)


@router.post("/final/gate/verify", dependencies=[rate_limit("final")])
async def final_gate_verify(
        body: FinalGateVerifyRequest | None = None,
        current: Authed = Depends(authed_mutation),
        plane: ControlPlane = Depends(get_plane)):
    """Explicit provider capability verification for the A80 gate.

    Performs real, bounded capability checks against the requested
    provider (or every configured provider when omitted), persists the
    machine-readable results, and returns the refreshed final gate.
    The gate itself never performs network calls; this endpoint is the
    explicit verification mechanism.
    """
    provider = (body.provider if body is not None else "") or ""
    return plane.final_gate_verify(current.session, provider)
