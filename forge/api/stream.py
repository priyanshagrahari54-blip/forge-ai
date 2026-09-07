"""Server-Sent Events live stream with cursor replay (A34).

``GET /api/v1/tasks/{task_id}/events/stream?after=<seq>`` first replays
missed events, then pushes live ones. Sequences are per-task monotonic,
so ``historical + live`` reconciles without duplicates on reconnect.

Authentication: ``Authorization: Bearer``, the session cookie (what the
browser's ``EventSource`` sends), or ``?token=`` for non-browser clients.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

from forge.api.deps import SESSION_COOKIE, get_plane, rate_limit
from forge.api.errors import error_body
from forge.control.control_plane import ControlPlane, TaskNotFound

router = APIRouter()

#: Bound one connection's lifetime so streams cannot grow forever.
MAX_STREAM_EVENTS = 2000


async def _resolve_session(request: Request,
                           plane: ControlPlane, token: str):
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    candidate = (value.strip() if scheme.lower() == "bearer"
                 and value.strip() else "")
    via = "bearer"
    if not candidate:
        candidate = request.cookies.get(SESSION_COOKIE, "")
        via = "cookie"
    if not candidate:
        candidate = token
        via = "query"
    session = (plane.sessions.get_by_token(candidate)
               if candidate else None)
    if session is None or not session.active:
        return None, via
    return session, via


@router.get("/tasks/{task_id}/events/stream",
            dependencies=[rate_limit("stream")])
async def event_stream(task_id: str, request: Request, after: int = 0,
                       token: str = "",
                       plane: ControlPlane = Depends(get_plane)):
    session, _ = await _resolve_session(request, plane, token)
    request_id = str(getattr(request.state, "request_id", "") or "")
    if session is None:
        return JSONResponse(
            status_code=401,
            content=error_body("AUTH_REQUIRED", "Authentication required.",
                               request_id))
    try:
        run = plane.get_task(session, task_id)
    except TaskNotFound:
        return JSONResponse(
            status_code=404,
            content=error_body("TASK_NOT_FOUND", "Unknown task.",
                               request_id))
    if run.project_id != session.project_id:  # pragma: no cover - guarded above
        return JSONResponse(
            status_code=404,
            content=error_body("TASK_NOT_FOUND", "Unknown task.",
                               request_id))
    cursor = max(0, int(after or 0))
    # EventSource reconnects send Last-Event-ID; honor it so a resumed
    # stream replays exactly what was missed.
    resumed = request.headers.get("last-event-id", "")
    if resumed:
        try:
            cursor = max(cursor, int(resumed))
        except ValueError:
            pass

    async def generate():
        nonlocal cursor
        yield "retry: 3000\n: connected\n\n"
        sent = 0
        idle_rounds = 0
        while sent < MAX_STREAM_EVENTS:
            if await request.is_disconnected():
                break
            # Re-validate periodically: revoked sessions lose the stream.
            idle_rounds += 1
            if idle_rounds % 12 == 0:
                live = await run_in_threadpool(
                    plane.sessions.get_by_token,
                    _current_token(request, token))
                if live is None or not live.active:
                    break
            events = await run_in_threadpool(
                plane.events.wait, run.id, cursor, 20.0)
            if not events:
                yield ": ping\n\n"
                continue
            for event in events:
                payload = json.dumps(event.to_dict(), default=str)
                yield (f"id: {event.seq}\nevent: {event.type}\n"
                       f"data: {payload}\n\n")
                cursor = event.seq
                sent += 1
                if sent >= MAX_STREAM_EVENTS:
                    break
        yield "event: end\ndata: {\"reason\": \"stream-closed\"}\n\n"

    return StreamingResponse(generate(),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _current_token(request: Request, query_token: str) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return request.cookies.get(SESSION_COOKIE, "") or query_token
