"""Backup & recovery API (A65): verified snapshots and restore."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import Authed, authed_mutation, get_plane, rate_limit
from forge.api.schemas import BackupCreateRequest
from forge.control.control_plane import ControlPlane, InvalidRequest

router = APIRouter()


@router.post("/backups", dependencies=[rate_limit("backup")])
async def create_backup(body: BackupCreateRequest,
                        current: Authed = Depends(authed_mutation),
                        plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.backup_create(current.session, body.label)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/backups")
async def list_backups(current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    return plane.backup_list(current.session)


@router.get("/backups/{backup_id}")
async def get_backup(backup_id: str,
                     current: Authed = Depends(authed_mutation),
                     plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.backup_get(current.session, backup_id)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/backups/{backup_id}/verify",
             dependencies=[rate_limit("backup")])
async def verify_backup(backup_id: str,
                        current: Authed = Depends(authed_mutation),
                        plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.backup_verify(current.session, backup_id)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/backups/{backup_id}/restore",
             dependencies=[rate_limit("backup")])
async def restore_backup(backup_id: str,
                         current: Authed = Depends(authed_mutation),
                         plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.backup_restore(current.session, backup_id)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
