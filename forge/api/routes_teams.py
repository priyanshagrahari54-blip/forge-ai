"""Agent teams API (A52): ordered teams of runtime-defined agents."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import Authed, authed_mutation, get_plane, rate_limit
from forge.api.schemas import TeamCreateRequest, TeamExecuteRequest
from forge.control.control_plane import ControlPlane, InvalidRequest

router = APIRouter()


@router.post("/teams", dependencies=[rate_limit("teams")])
async def create_team(body: TeamCreateRequest,
                      current: Authed = Depends(authed_mutation),
                      plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.team_create(current.session, body.name,
                                 list(body.members))
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/teams")
async def list_teams(current: Authed = Depends(authed_mutation),
                     plane: ControlPlane = Depends(get_plane)):
    return plane.team_list(current.session)


@router.post("/teams/{team_id}/execute",
             dependencies=[rate_limit("teams")])
async def execute_team(team_id: str, body: TeamExecuteRequest,
                       current: Authed = Depends(authed_mutation),
                       plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.team_execute(current.session, team_id,
                                  body.requirement,
                                  approval_id=body.approval_id)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/teams/{team_id}/result/{run_id}")
async def team_result(team_id: str, run_id: str,
                      current: Authed = Depends(authed_mutation),
                      plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.team_run_result(current.session, team_id, run_id)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
