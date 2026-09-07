"""Approval routes (A34). Decisions hit the A33 store via the adapter."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from forge.api.deps import (
    Authed,
    authed,
    authed_mutation,
    get_plane,
    rate_limit,
)
from forge.control.control_plane import ControlPlane

router = APIRouter()


@router.get("/approvals")
async def list_approvals(current: Authed = Depends(authed),
                         plane: ControlPlane = Depends(get_plane)):
    return {"approvals": plane.list_approvals(current.session)}


@router.get("/approvals/{approval_id}")
async def get_approval(approval_id: str,
                       current: Authed = Depends(authed),
                       plane: ControlPlane = Depends(get_plane)):
    return {"approval": plane.get_approval(current.session, approval_id)}


@router.post("/approvals/{approval_id}/approve",
             dependencies=[rate_limit("approvals")])
async def approve(approval_id: str,
                  current: Authed = Depends(authed_mutation),
                  plane: ControlPlane = Depends(get_plane)):
    return {"approval": plane.approve_request(
        current.session, approval_id)}


@router.post("/approvals/{approval_id}/deny",
             dependencies=[rate_limit("approvals")])
async def deny(approval_id: str,
               current: Authed = Depends(authed_mutation),
               plane: ControlPlane = Depends(get_plane)):
    return {"approval": plane.deny_request(current.session, approval_id)}


# -- aliases --------------------------------------------------------------------


@router.get("/permission-requests")
async def list_permission_requests(
        current: Authed = Depends(authed),
        plane: ControlPlane = Depends(get_plane)):
    return {"approvals": plane.list_approvals(current.session)}


@router.post("/permission-requests/{approval_id}/approve",
             dependencies=[rate_limit("approvals")])
async def approve_alias(approval_id: str,
                        current: Authed = Depends(authed_mutation),
                        plane: ControlPlane = Depends(get_plane)):
    return {"approval": plane.approve_request(
        current.session, approval_id)}


@router.post("/permission-requests/{approval_id}/deny",
             dependencies=[rate_limit("approvals")])
async def deny_alias(approval_id: str,
                     current: Authed = Depends(authed_mutation),
                     plane: ControlPlane = Depends(get_plane)):
    return {"approval": plane.deny_request(current.session, approval_id)}
