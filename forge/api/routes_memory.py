"""Persistent session/project memory API (A37).

Every memory access passes the A33 permission gate (Resource.MEMORY
read/write/delete); writes and deletes additionally go through the same
approval store as every other surface (single-use tokens). Memory is
session-scoped: a session sees exactly its own entries and the project
keys its policy allows.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from forge.api.deps import (
    Authed,
    authed,
    authed_mutation,
    get_plane,
    rate_limit,
)
from forge.api.schemas import (
    MemoryDeleteRequest,
    MemoryProjectSaveRequest,
    MemorySaveRequest,
)
from forge.control.control_plane import ControlPlane

router = APIRouter()


@router.get("/memory")
async def memory_overview(current: Authed = Depends(authed),
                          plane: ControlPlane = Depends(get_plane)):
    return plane.memory_overview(current.session)


@router.post("/memory", dependencies=[rate_limit("memory")])
async def memory_add(body: MemorySaveRequest,
                     current: Authed = Depends(authed_mutation),
                     plane: ControlPlane = Depends(get_plane)):
    return plane.memory_add(current.session, body.kind, body.content,
                            approval_id=body.approval_id)


@router.get("/memory/entries/{entry_id}")
async def memory_get(entry_id: str,
                     current: Authed = Depends(authed),
                     plane: ControlPlane = Depends(get_plane),
                     approval_id: str = Query(default="", max_length=128)):
    return plane.memory_get(current.session, entry_id,
                            approval_id=approval_id)


@router.post("/memory/delete", dependencies=[rate_limit("memory")])
async def memory_delete(body: MemoryDeleteRequest,
                        current: Authed = Depends(authed_mutation),
                        plane: ControlPlane = Depends(get_plane)):
    return plane.memory_delete(current.session, body.entry_id,
                               approval_id=body.approval_id)


@router.get("/memory/project")
async def memory_project_list(current: Authed = Depends(authed),
                              plane: ControlPlane = Depends(get_plane)):
    return {"project_keys": plane.memory_overview(
        current.session)["project_keys"]}


@router.post("/memory/project/save", dependencies=[rate_limit("memory")])
async def memory_project_save(body: MemoryProjectSaveRequest,
                              current: Authed = Depends(authed_mutation),
                              plane: ControlPlane = Depends(get_plane)):
    return plane.memory_project_save(current.session, body.key,
                                     body.content,
                                     approval_id=body.approval_id)


@router.get("/memory/project/get")
async def memory_project_get(current: Authed = Depends(authed),
                             plane: ControlPlane = Depends(get_plane),
                             key: str = Query(min_length=1,
                                              max_length=256)):
    return plane.memory_project_load(current.session, key)


@router.get("/memory/approvals")
async def memory_approvals(current: Authed = Depends(authed),
                           plane: ControlPlane = Depends(get_plane)):
    return {"approvals": plane.list_memory_approvals(current.session)}


@router.post("/memory/approvals/{approval_id}/approve",
             dependencies=[rate_limit("memory")])
async def memory_approve(approval_id: str,
                         current: Authed = Depends(authed_mutation),
                         plane: ControlPlane = Depends(get_plane)):
    return plane.decide_memory_request(current.session, approval_id, True)


@router.post("/memory/approvals/{approval_id}/deny",
             dependencies=[rate_limit("memory")])
async def memory_deny(approval_id: str,
                      current: Authed = Depends(authed_mutation),
                      plane: ControlPlane = Depends(get_plane)):
    return plane.decide_memory_request(current.session, approval_id, False)
