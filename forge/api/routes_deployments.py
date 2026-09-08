"""Deployment API (A64): manifests, builds, deploy, rollback."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import Authed, authed_mutation, get_plane, rate_limit
from forge.api.schemas import (DeploymentCreateRequest,
                               DeploymentDeployRequest)
from forge.control.control_plane import ControlPlane, InvalidRequest

router = APIRouter()


@router.post("/deployments", dependencies=[rate_limit("deployment")])
async def create_deployment(body: DeploymentCreateRequest,
                            current: Authed = Depends(authed_mutation),
                            plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.deployment_create(current.session, body.name,
                                       body.version)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/deployments")
async def list_deployments(current: Authed = Depends(authed_mutation),
                           plane: ControlPlane = Depends(get_plane)):
    return plane.deployment_list(current.session)


@router.get("/deployments/{deployment_id}")
async def get_deployment(deployment_id: str,
                         current: Authed = Depends(authed_mutation),
                         plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.deployment_get(current.session, deployment_id)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/deployments/{deployment_id}/build",
             dependencies=[rate_limit("deployment")])
async def build_deployment(deployment_id: str,
                           current: Authed = Depends(authed_mutation),
                           plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.deployment_build(current.session, deployment_id)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/deployments/{deployment_id}/deploy",
             dependencies=[rate_limit("deployment")])
async def deploy_deployment(deployment_id: str,
                            body: DeploymentDeployRequest,
                            current: Authed = Depends(authed_mutation),
                            plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.deployment_deploy(current.session, deployment_id,
                                       body.target)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/deployments/{deployment_id}/rollback",
             dependencies=[rate_limit("deployment")])
async def rollback_deployment(deployment_id: str,
                              current: Authed = Depends(authed_mutation),
                              plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.deployment_rollback(current.session, deployment_id)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
