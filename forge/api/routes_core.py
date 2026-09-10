"""Health, session, project, and dashboard routes (A34)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from forge.api.deps import (
    SESSION_COOKIE,
    Authed,
    authed,
    authed_mutation,
    get_plane,
    rate_limit,
)
from forge.api.schemas import CreateSessionRequest
from forge.control.control_plane import (
    ControlPlane,
    ProjectNotFound,
)

router = APIRouter()


@router.get("/health")
async def health(plane: ControlPlane = Depends(get_plane)):
    return plane.health()


@router.get("/health/models")
async def health_models(_: Authed = Depends(authed),
                        plane: ControlPlane = Depends(get_plane)):
    return plane.model_health()


@router.post("/sessions", dependencies=[rate_limit("sessions")])
async def create_session(body: CreateSessionRequest, response: Response,
                         request: Request,
                         plane: ControlPlane = Depends(get_plane)):
    session, token = plane.create_session(
        body.actor, body.project_id, profile=body.profile or "assisted")
    secure = bool(request.app.state.secure_cookies)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax", path="/",
        max_age=int(plane.config.session_ttl), secure=secure)
    return {"session": session.to_dict(), "token": token}


@router.get("/sessions/me")
async def session_me(current: Authed = Depends(authed)):
    return {"session": current.session.to_dict()}


@router.delete("/sessions/me")
async def session_revoke(response: Response,
                         current: Authed = Depends(authed_mutation),
                         plane: ControlPlane = Depends(get_plane)):
    plane.revoke_session(current.session)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"revoked": True}


@router.get("/dashboard")
async def dashboard(current: Authed = Depends(authed),
                    plane: ControlPlane = Depends(get_plane)):
    return plane.dashboard(current.session)


@router.get("/projects")
async def list_projects(current: Authed = Depends(authed),
                        plane: ControlPlane = Depends(get_plane)):
    del current  # listing registered projects reveals metadata only
    return {"projects": plane.list_projects()}


@router.get("/projects/{project_id}")
async def project_state(project_id: str,
                        current: Authed = Depends(authed),
                        plane: ControlPlane = Depends(get_plane)):
    if project_id != current.session.project_id:
        raise ProjectNotFound(f"Unknown project: {project_id!r}")
    return plane.get_project_state(current.session)


@router.get("/projects/{project_id}/git")
async def project_git(project_id: str,
                      current: Authed = Depends(authed),
                      plane: ControlPlane = Depends(get_plane)):
    if project_id != current.session.project_id:
        raise ProjectNotFound(f"Unknown project: {project_id!r}")
    return plane.git_status(current.session)


@router.get("/projects/{project_id}/git/diff")
async def project_git_diff(project_id: str, staged: bool = False,
                           current: Authed = Depends(authed),
                           plane: ControlPlane = Depends(get_plane)):
    if project_id != current.session.project_id:
        raise ProjectNotFound(f"Unknown project: {project_id!r}")
    return plane.git_diff(current.session, staged=staged)


@router.post("/recover")
async def recover(current: Authed = Depends(authed_mutation),
                  plane: ControlPlane = Depends(get_plane)):
    return plane.request_recovery(current.session)
