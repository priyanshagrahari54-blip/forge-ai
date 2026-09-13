"""Research API (A47): evidence-based answers about the codebase + web."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forge.api.deps import Authed, authed_mutation, get_plane, rate_limit
from forge.api.schemas import ResearchQueryRequest, ResearchQuestionRequest
from forge.control.control_plane import ControlPlane, InvalidRequest

router = APIRouter()


@router.post("/research/ask", dependencies=[rate_limit("research")])
async def ask(body: ResearchQuestionRequest,
              current: Authed = Depends(authed_mutation),
              plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.research_ask(current.session, body.question)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/research/report")
async def report(current: Authed = Depends(authed_mutation),
                 plane: ControlPlane = Depends(get_plane)):
    return plane.research_report(current.session)


@router.post("/research/web", dependencies=[rate_limit("research")])
async def web_search(body: ResearchQuestionRequest,
                     current: Authed = Depends(authed_mutation),
                     plane: ControlPlane = Depends(get_plane)):
    try:
        return plane.research_web(current.session, body.question)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/research/fetch", dependencies=[rate_limit("research")])
async def fetch_url(body: dict,
                    current: Authed = Depends(authed_mutation),
                    plane: ControlPlane = Depends(get_plane)):
    try:
        url = body.get("url", "")
        return plane.research_fetch(current.session, url)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/research/query", dependencies=[rate_limit("research")])
async def query(body: ResearchQueryRequest,
                current: Authed = Depends(authed_mutation),
                plane: ControlPlane = Depends(get_plane)):
    """Secure multi-source research; every result carries provenance."""
    try:
        return plane.research_query(
            current.session, body.question, allow_web=body.allow_web,
            sources=tuple(s[:64] for s in body.sources),
            user_notes=tuple(n[:4000] for n in body.user_notes),
            allow_model_knowledge=body.allow_model_knowledge)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/research/status")
async def status(current: Authed = Depends(authed_mutation),
                 plane: ControlPlane = Depends(get_plane)):
    return plane.research_status(current.session)
