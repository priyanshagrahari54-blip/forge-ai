"""Desktop-client link API (A81): authenticated Forge Desktop ↔ server.

Mounted by the cockpit app under ``/api/v1/link/*``. Every route is a
fixed, typed operation — there is **no** endpoint that accepts commands,
code, or paths from the client beyond the existing task submission
vocabulary (server authorization stays authoritative).

Authentication is the A81 signature scheme (:mod:`forge.link.protocol`):

- ``POST /link/challenge`` and ``POST /link/handshake`` are the only
  unsigned routes (a handshake reveals only random nonces);
- everything else must carry ``X-Forge-Client`` / ``X-Forge-Timestamp``
  / ``X-Forge-Nonce`` / ``X-Forge-Signature`` covering the exact method,
  path, and raw body bytes, with a fresh single-use nonce inside the
  replay window.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from forge.api.errors import error_body
from forge.control.control_plane import ControlPlane
from forge.control.sessions import Session
from forge.link import protocol
from forge.link.errors import AuthError, AuthExpired, LinkError, RequestError
from forge.server.service import LinkService

router = APIRouter()

#: Bound on link JSON bodies (defense in depth; the app-level middleware
#: already enforces a larger global cap).
MAX_LINK_BODY_BYTES = 256 * 1024


@dataclass(frozen=True)
class LinkAuthed:
    """Verified link request: the bound ControlPlane session + client id."""
    session: Session
    client_id: str


def get_link_service(request: Request) -> LinkService:
    service = getattr(request.app.state, "link", None)
    if service is None:  # routes mounted without the link service
        raise RequestError("link not configured", code="LINK_DISABLED")
    return service


async def link_authed(request: Request) -> LinkAuthed:
    """Dependency: verify the A81 signature; yield the bound session."""
    service = get_link_service(request)
    body = await request.body()
    if len(body) > MAX_LINK_BODY_BYTES:
        raise RequestError("body too large", code="INVALID_REQUEST")
    client_id = request.headers.get(protocol.HEADER_CLIENT, "")
    timestamp = request.headers.get(protocol.HEADER_TIMESTAMP, "")
    nonce = request.headers.get(protocol.HEADER_NONCE, "")
    signature = request.headers.get(protocol.HEADER_SIGNATURE, "")
    # The signature covers path + query exactly as the client built it.
    signed_path = request.url.path
    if request.url.query:
        signed_path = f"{signed_path}?{request.url.query}"
    session = service.verify_request(
        client_id, timestamp, nonce, signature,
        request.method, signed_path, body)
    return LinkAuthed(session=session, client_id=client_id)


def _json(payload: bytes) -> Dict[str, Any]:
    try:
        parsed = json.loads(payload.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        raise RequestError("invalid JSON body",
                           code="INVALID_REQUEST") from None
    if not isinstance(parsed, dict):
        raise RequestError("JSON body must be an object",
                           code="INVALID_REQUEST")
    return parsed


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or "")


def _error_response(request: Request, exc: LinkError) -> JSONResponse:
    if isinstance(exc, (AuthError, AuthExpired)):
        status = 401
    elif exc.code.endswith("_NOT_FOUND"):
        status = 404
    elif exc.code in ("CLIENT_EXISTS", "APPROVAL_CONFLICT", "CONFLICT"):
        status = 409
    else:
        status = 400
    return JSONResponse(
        status_code=status,
        content=error_body(exc.code, str(exc) or exc.code,
                           _request_id(request)))


# -- handshake (unsigned) -------------------------------------------------

@router.post("/link/challenge")
async def challenge(request: Request,
                    service: LinkService = Depends(get_link_service)):
    body = await request.body()
    if len(body) > MAX_LINK_BODY_BYTES:
        return _error_response(
            request, RequestError("body too large", code="INVALID_REQUEST"))
    parsed = _json(body)
    return service.challenge(str(parsed.get("client_id", "")),
                             str(parsed.get("nonce", "")))


@router.post("/link/handshake")
async def handshake(request: Request,
                    service: LinkService = Depends(get_link_service)):
    body = await request.body()
    if len(body) > MAX_LINK_BODY_BYTES:
        return _error_response(
            request, RequestError("body too large", code="INVALID_REQUEST"))
    parsed = _json(body)
    return service.handshake(str(parsed.get("client_id", "")),
                             str(parsed.get("nonce", "")),
                             str(parsed.get("proof", "")))


# -- tasks ------------------------------------------------------------------

@router.post("/link/tasks")
async def submit_task(request: Request,
                      current: LinkAuthed = Depends(link_authed),
                      service: LinkService = Depends(get_link_service)):
    body = await request.body()
    parsed = _json(body)
    return service.submit_task(
        current.session, current.client_id,
        str(parsed.get("requirement", "")),
        mode=str(parsed.get("mode", "") or ""),
        execution=str(parsed.get("execution", "SERVER") or "SERVER")[:16],
        decision=str(parsed.get("decision", "") or "")[:200])


@router.get("/link/tasks")
async def list_tasks(request: Request,
                     current: LinkAuthed = Depends(link_authed),
                     service: LinkService = Depends(get_link_service)):
    return {"tasks": service.list_tasks(
        current.session,
        status=request.query_params.get("status", ""),
        limit=int(request.query_params.get("limit", "50") or "50"))}


@router.get("/link/tasks/{task_id}")
async def get_task(task_id: str,
                   current: LinkAuthed = Depends(link_authed),
                   service: LinkService = Depends(get_link_service)):
    return {"task": service.get_task(current.session, task_id)}


@router.get("/link/tasks/{task_id}/logs")
async def task_logs(task_id: str,
                    current: LinkAuthed = Depends(link_authed),
                    service: LinkService = Depends(get_link_service)):
    return service.get_task_logs(current.session, task_id)


@router.get("/link/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request,
                      current: LinkAuthed = Depends(link_authed),
                      service: LinkService = Depends(get_link_service)):
    after = int(request.query_params.get("after", "0") or "0")
    events, latest = service.get_task_events(current.session, task_id,
                                             after=after)
    return {"events": events, "cursor": latest}


@router.get("/link/tasks/{task_id}/verification")
async def task_verification(task_id: str,
                            current: LinkAuthed = Depends(link_authed),
                            service: LinkService = Depends(get_link_service)):
    return service.get_verification(current.session, task_id)


@router.get("/link/tasks/{task_id}/report")
async def task_report(task_id: str,
                      current: LinkAuthed = Depends(link_authed),
                      service: LinkService = Depends(get_link_service)):
    return service.get_task_report(current.session, task_id)


def _mutate(operation: str):
    async def handler(task_id: str, request: Request,
                      current: LinkAuthed = Depends(link_authed),
                      service: LinkService = Depends(get_link_service)):
        return service.mutate_task(current.session, operation, task_id)
    handler.__name__ = f"link_task_{operation}"
    return handler


router.post("/link/tasks/{task_id}/pause")(_mutate("pause"))
router.post("/link/tasks/{task_id}/resume")(_mutate("resume"))
router.post("/link/tasks/{task_id}/cancel")(_mutate("cancel"))
router.post("/link/tasks/{task_id}/retry")(_mutate("retry"))


# -- approvals ----------------------------------------------------------------

@router.get("/link/approvals")
async def list_approvals(current: LinkAuthed = Depends(link_authed),
                         service: LinkService = Depends(get_link_service)):
    return {"approvals": service.list_approvals(current.session)}


@router.post("/link/approvals/{approval_id}/decide")
async def decide_approval(approval_id: str, request: Request,
                          current: LinkAuthed = Depends(link_authed),
                          service: LinkService = Depends(get_link_service)):
    body = await request.body()
    parsed = _json(body)
    approved = parsed.get("approved")
    if not isinstance(approved, bool):
        raise RequestError("'approved' must be a boolean",
                           code="INVALID_REQUEST")
    return service.decide_approval(current.session, approval_id, approved)


# -- state snapshot + info ------------------------------------------------------

@router.get("/link/state")
async def link_state(request: Request,
                     current: LinkAuthed = Depends(link_authed),
                     service: LinkService = Depends(get_link_service)):
    since = int(request.query_params.get("since", "0") or "0")
    focus = request.query_params.get("task", "")
    return service.state_snapshot(current.session, since_seq=since,
                                  selected_task=focus)


@router.get("/link/info")
async def link_info(current: LinkAuthed = Depends(link_authed),
                    service: LinkService = Depends(get_link_service)):
    info = service.server_info()
    info["client_id"] = current.client_id
    info["project_id"] = current.session.project_id
    info["expires_at"] = current.session.expires_at
    return info


def install_link_handler(app, service: Optional[LinkService]) -> None:
    """Attach the link service + LinkError mapping to the cockpit app."""
    app.state.link = service

    async def _link_error(request: Request, exc: LinkError) -> JSONResponse:
        return _error_response(request, exc)

    app.add_exception_handler(LinkError, _link_error)  # type: ignore[arg-type]


def build_link_service(plane: ControlPlane) -> LinkService:
    return LinkService(plane)
