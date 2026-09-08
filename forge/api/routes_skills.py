"""Agent skills API (A54): declarative validated skills."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import Authed, authed_mutation, get_plane, rate_limit
from forge.api.schemas import (SkillAttachRequest, SkillCreateRequest)
from forge.control.control_plane import ControlPlane, InvalidRequest

router = APIRouter()


@router.post("/skills", dependencies=[rate_limit("skills")])
async def create_skill(body: SkillCreateRequest,
                       current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.skill_create(current.session, body.name,
                                  body.capability,
                                  description=body.description,
                                  version=body.version)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/skills")
async def list_skills(current: Authed = Depends(authed_mutation),
                      plane: ControlPlane = Depends(get_plane)):
    return plane.skill_list(current.session)


@router.post("/agents/{name}/skills", dependencies=[rate_limit("skills")])
async def attach_skill(name: str, body: SkillAttachRequest,
                       current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.agent_attach_skill(current.session, name,
                                        body.skill)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.delete("/agents/{name}/skills/{skill}",
               dependencies=[rate_limit("skills")])
async def detach_skill(name: str, skill: str,
                       current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.agent_detach_skill(current.session, name, skill)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
