"""Controlled self-improvement API (A81).

Read endpoints expose the analysis and the ledger-backed dashboard. Mutating
endpoints build isolated candidates, record a human policy approval, apply
an approved candidate to the working tree (never committing), roll back, or
discard. Every call is session-authenticated, CSRF-checked when cookie-
authenticated, rate-limited, and audited by the control plane.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from forge.api.deps import Authed, authed_mutation, get_plane, rate_limit
from forge.api.schemas import (
    SelfImprovementAnalyzeRequest,
    SelfImprovementApproveRequest,
    SelfImprovementRunRequest,
)
from forge.control.control_plane import ControlPlane

router = APIRouter()


@router.get("/self-improvement")
async def self_improvement_dashboard(current: Authed = Depends(authed_mutation),
                                     plane: ControlPlane = Depends(get_plane)):
    return plane.self_improvement_dashboard(current.session)


@router.post("/self-improvement/analyze",
             dependencies=[rate_limit("self_improvement")])
async def self_improvement_analyze(body: SelfImprovementAnalyzeRequest,
                                   current: Authed = Depends(authed_mutation),
                                   plane: ControlPlane = Depends(get_plane)):
    return plane.self_improvement_analyze(current.session,
                                          run_tests=body.run_tests)


@router.post("/self-improvement/run",
             dependencies=[rate_limit("self_improvement")])
async def self_improvement_run(body: SelfImprovementRunRequest,
                               current: Authed = Depends(authed_mutation),
                               plane: ControlPlane = Depends(get_plane)):
    return plane.self_improvement_run(current.session,
                                      iterations=body.iterations,
                                      run_tests=body.run_tests,
                                      targets=body.targets)


@router.post("/self-improvement/candidates/{candidate_id}/approve",
             dependencies=[rate_limit("approvals")])
async def self_improvement_approve(candidate_id: str,
                                   body: SelfImprovementApproveRequest,
                                   current: Authed = Depends(authed_mutation),
                                   plane: ControlPlane = Depends(get_plane)):
    return plane.self_improvement_approve(
        current.session, candidate_id, reason=body.reason,
        change_fingerprint=body.change_fingerprint)


@router.post("/self-improvement/candidates/{candidate_id}/apply",
             dependencies=[rate_limit("approvals")])
async def self_improvement_apply(candidate_id: str,
                                 current: Authed = Depends(authed_mutation),
                                 plane: ControlPlane = Depends(get_plane)):
    return plane.self_improvement_apply(current.session, candidate_id)


@router.post("/self-improvement/candidates/{candidate_id}/rollback",
             dependencies=[rate_limit("approvals")])
async def self_improvement_rollback(candidate_id: str,
                                    current: Authed = Depends(authed_mutation),
                                    plane: ControlPlane = Depends(get_plane)):
    return plane.self_improvement_rollback(current.session, candidate_id)


@router.delete("/self-improvement/candidates/{candidate_id}",
               dependencies=[rate_limit("approvals")])
async def self_improvement_discard(candidate_id: str,
                                   current: Authed = Depends(authed_mutation),
                                   plane: ControlPlane = Depends(get_plane)):
    return plane.self_improvement_discard(current.session, candidate_id)
