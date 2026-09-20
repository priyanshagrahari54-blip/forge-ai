"""Personal-assistant API (A84).

Conversation, session continuity, memory user-controls, profile, prompt
ledger, tool registry, deep research, networks status, patterns, learning,
improvement, verification, model teams and the declared-scale catalog —
every surface is the control plane's own assistant plane (no second
implementation), every mutation is CSRF-authenticated and rate-limited, and
memory mutations pass the same A33 memory permission gate (and approval
store) that A37 memory uses.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from forge.api.deps import Authed, authed, authed_mutation, get_plane, rate_limit
from forge.api.schemas import (
    AssistantAdjudicateRequest, AssistantContinuityRequest,
    AssistantCritiqueRequest, AssistantMemoryClearRequest,
    AssistantMemoryCorrectRequest, AssistantMemoryIdRequest,
    AssistantNetworkGoalRequest, AssistantNetworkUrlRequest,
    AssistantProfileSetRequest, AssistantResearchRequest,
    AssistantRespondRequest, AssistantRetentionRequest, AssistantTeamRunRequest,
    AssistantTextRequest,
)
from forge.control.control_plane import ControlPlane

router = APIRouter()


def _ap(plane: ControlPlane):
    return plane.assistant


# -- conversation ------------------------------------------------------------------

@router.post("/assistant/respond", dependencies=[rate_limit("assistant")])
async def assistant_respond(body: AssistantRespondRequest,
                            current: Authed = Depends(authed_mutation),
                            plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).respond(
        current.session, body.message,
        assistant_session_id=body.session_id, allow_web=body.allow_web,
        confirmed=body.confirmed)


@router.get("/assistant/sessions")
async def assistant_sessions(current: Authed = Depends(authed),
                             plane: ControlPlane = Depends(get_plane),
                             limit: int = Query(default=25, ge=1, le=100)):
    return _ap(plane).sessions(limit=limit)


@router.get("/assistant/sessions/{assistant_session_id}")
async def assistant_session_state(assistant_session_id: str,
                                  current: Authed = Depends(authed),
                                  plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).session_state(current.session,
                                    assistant_session_id[:64])


@router.post("/assistant/sessions/{assistant_session_id}/retention",
             dependencies=[rate_limit("assistant")])
async def assistant_retention(assistant_session_id: str,
                              body: AssistantRetentionRequest,
                              current: Authed = Depends(authed_mutation),
                              plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).set_retention(assistant_session_id[:64], body.mode,
                                    session=current.session)


@router.post("/assistant/continuity", dependencies=[rate_limit("assistant")])
async def assistant_continuity(body: AssistantContinuityRequest,
                               current: Authed = Depends(authed_mutation),
                               plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).continuity(body.session_id[:64], body.text)


# -- memory user controls -------------------------------------------------------------

@router.get("/assistant/memory")
async def assistant_memory_search(current: Authed = Depends(authed),
                                  plane: ControlPlane = Depends(get_plane),
                                  q: str = Query(default="", max_length=400),
                                  k: int = Query(default=8, ge=1, le=50)):
    ap = _ap(plane)
    if q.strip():
        return ap.memory_search(q, k=k)
    return ap.memory_inspect(limit=k)


@router.get("/assistant/memory/short-term")
async def assistant_memory_short_term(current: Authed = Depends(authed),
                                      plane: ControlPlane = Depends(get_plane),
                                      session_id: str = Query(default="",
                                                               max_length=64)):
    return _ap(plane).memory_short_term(session_id[:64])


@router.get("/assistant/memory/{entry_id}/provenance")
async def assistant_memory_provenance(entry_id: str,
                                      current: Authed = Depends(authed),
                                      plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).memory_provenance(entry_id[:128])


@router.post("/assistant/memory/correct", dependencies=[rate_limit("assistant")])
async def assistant_memory_correct(body: AssistantMemoryCorrectRequest,
                                   current: Authed = Depends(authed_mutation),
                                   plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).memory_correct(
        body.entry_id, body.content, session=current.session,
        approval_id=body.approval_id)


@router.post("/assistant/memory/delete", dependencies=[rate_limit("assistant")])
async def assistant_memory_delete(body: AssistantMemoryIdRequest,
                                  current: Authed = Depends(authed_mutation),
                                  plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).memory_delete(
        body.entry_id, session=current.session,
        approval_id=body.approval_id)


@router.post("/assistant/memory/forget", dependencies=[rate_limit("assistant")])
async def assistant_memory_forget(body: AssistantMemoryIdRequest,
                                  current: Authed = Depends(authed_mutation),
                                  plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).memory_forget(
        body.entry_id, session=current.session,
        approval_id=body.approval_id)


@router.post("/assistant/memory/clear", dependencies=[rate_limit("assistant")])
async def assistant_memory_clear(body: AssistantMemoryClearRequest,
                                 current: Authed = Depends(authed_mutation),
                                 plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).memory_clear(
        body.confirm, session=current.session, approval_id=body.approval_id)


# -- profile ---------------------------------------------------------------------------

@router.get("/assistant/profile")
async def assistant_profile(current: Authed = Depends(authed),
                            plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).profile()


@router.post("/assistant/profile", dependencies=[rate_limit("assistant")])
async def assistant_profile_set(body: AssistantProfileSetRequest,
                                current: Authed = Depends(authed_mutation),
                                plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).profile_set(
        body.field, body.value, session=current.session,
        approval_id=body.approval_id)


@router.get("/assistant/profile/proposals")
async def assistant_profile_proposals(current: Authed = Depends(authed),
                                      plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).profile_proposals()


# -- prompts ---------------------------------------------------------------------------

@router.get("/assistant/prompts")
async def assistant_prompt_versions(current: Authed = Depends(authed),
                                    plane: ControlPlane = Depends(get_plane),
                                    session_id: str = Query(default="",
                                                             max_length=64),
                                    limit: int = Query(default=20, ge=1, le=100)):
    return _ap(plane).prompt_versions(session_id[:64], limit=limit)


@router.get("/assistant/prompts/strategies")
async def assistant_prompt_strategies(current: Authed = Depends(authed),
                                      plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).prompt_strategy_stats()


# -- tools / research / networks ----------------------------------------------------------

@router.get("/assistant/tools")
async def assistant_tools(current: Authed = Depends(authed),
                          plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).tool_catalog()


@router.post("/assistant/tools/plan", dependencies=[rate_limit("assistant")])
async def assistant_tool_plan(body: AssistantTextRequest,
                              current: Authed = Depends(authed_mutation),
                              plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).tool_plan(body.text)


@router.post("/assistant/research", dependencies=[rate_limit("assistant")])
async def assistant_research(body: AssistantResearchRequest,
                             current: Authed = Depends(authed_mutation),
                             plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).research_deep(body.question, allow_web=body.allow_web)


@router.get("/assistant/networks")
async def assistant_networks(current: Authed = Depends(authed),
                             plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).networks_status()


@router.post("/assistant/networks/classify", dependencies=[rate_limit("assistant")])
async def assistant_network_classify(body: AssistantNetworkUrlRequest,
                                     current: Authed = Depends(authed_mutation),
                                     plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).network_classify(body.url)


@router.post("/assistant/networks/screen", dependencies=[rate_limit("assistant")])
async def assistant_network_screen(body: AssistantNetworkGoalRequest,
                                   current: Authed = Depends(authed_mutation),
                                   plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).network_screen(body.goal)


# -- patterns / learning / improvement ------------------------------------------------------

@router.get("/assistant/patterns")
async def assistant_patterns(current: Authed = Depends(authed),
                             plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).pattern_stats()


@router.post("/assistant/patterns/adjudicate",
             dependencies=[rate_limit("assistant")])
async def assistant_pattern_adjudicate(body: AssistantAdjudicateRequest,
                                       current: Authed = Depends(authed_mutation),
                                       plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).pattern_adjudicate(body.conflict_id, body.resolution,
                                          note=body.note)


@router.get("/assistant/learning")
async def assistant_learning(current: Authed = Depends(authed),
                             plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).learning_stats()


@router.post("/assistant/improvement/scan", dependencies=[rate_limit("assistant")])
async def assistant_improvement_scan(current: Authed = Depends(authed_mutation),
                                     plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).improvement_scan()


# -- verification / teams / scale catalog -----------------------------------------------------

@router.post("/assistant/verify", dependencies=[rate_limit("assistant")])
async def assistant_verify(body: AssistantCritiqueRequest,
                           current: Authed = Depends(authed_mutation),
                           plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).critique_review(
        body.text, requirements=list(body.requirements)[:40],
        citations=list(body.citations)[:40])


@router.post("/assistant/teams/run", dependencies=[rate_limit("assistant")])
async def assistant_team_run(body: AssistantTeamRunRequest,
                             current: Authed = Depends(authed_mutation),
                             plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).team_run(body.task, shape=body.shape)


@router.get("/assistant/models/scale")
async def assistant_model_scale(current: Authed = Depends(authed),
                                plane: ControlPlane = Depends(get_plane)):
    return _ap(plane).model_scale_catalog()
