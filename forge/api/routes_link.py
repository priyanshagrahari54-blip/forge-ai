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

from forge.api.deps import rate_limit
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


def _int_param(request: Request, name: str, default: int, *,
               low: int = 0, high: int = 100000) -> int:
    """Query param -> bounded int; garbage is a 400, never a 500."""
    raw = request.query_params.get(name, "")
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RequestError(f"{name} must be an integer",
                           code="INVALID_REQUEST") from None
    if value < low or value > high:
        raise RequestError(
            f"{name} out of range ({low}..{high})",
            code="INVALID_REQUEST")
    return value


#: Honest execution labels a client may declare (the server records the
#: origin; HYBRID resolves to one of these on the client before submit).
_EXECUTION_LABELS = ("LOCAL", "SERVER", "LOCAL-PLANE")


def _execution_label(raw: str) -> str:
    value = (raw or "").strip().upper()
    return value if value in _EXECUTION_LABELS else "SERVER"


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

@router.post("/link/challenge", dependencies=[rate_limit("link")])
async def challenge(request: Request,
                    service: LinkService = Depends(get_link_service)):
    body = await request.body()
    if len(body) > MAX_LINK_BODY_BYTES:
        return _error_response(
            request, RequestError("body too large", code="INVALID_REQUEST"))
    parsed = _json(body)
    client_id = parsed.get("client_id", "")
    nonce = parsed.get("nonce", "")
    if not isinstance(client_id, str) or not isinstance(nonce, str):
        return _error_response(
            request, RequestError("client_id and nonce must be strings",
                                  code="INVALID_REQUEST"))
    return service.challenge(client_id, nonce)


@router.post("/link/handshake", dependencies=[rate_limit("link")])
async def handshake(request: Request,
                    service: LinkService = Depends(get_link_service)):
    body = await request.body()
    if len(body) > MAX_LINK_BODY_BYTES:
        return _error_response(
            request, RequestError("body too large", code="INVALID_REQUEST"))
    parsed = _json(body)
    client_id = parsed.get("client_id", "")
    nonce = parsed.get("nonce", "")
    proof = parsed.get("proof", "")
    if not all(isinstance(v, str) for v in (client_id, nonce, proof)):
        return _error_response(
            request, RequestError(
                "client_id, nonce and proof must be strings",
                code="INVALID_REQUEST"))
    return service.handshake(client_id, nonce, proof)


# -- tasks ------------------------------------------------------------------

@router.post("/link/tasks")
async def submit_task(request: Request,
                      current: LinkAuthed = Depends(link_authed),
                      service: LinkService = Depends(get_link_service)):
    body = await request.body()
    parsed = _json(body)
    requirement = parsed.get("requirement", "")
    mode = parsed.get("mode", "")
    decision = parsed.get("decision", "")
    if not isinstance(requirement, str) or not isinstance(mode, str) \
            or not isinstance(decision, str):
        return _error_response(
            request, RequestError(
                "requirement, mode and decision must be strings",
                code="INVALID_REQUEST"))
    return service.submit_task(
        current.session, current.client_id, requirement,
        mode=mode,
        execution=_execution_label(
            str(parsed.get("execution", "SERVER") or "SERVER")),
        decision=decision[:200])


@router.get("/link/tasks")
async def list_tasks(request: Request,
                     current: LinkAuthed = Depends(link_authed),
                     service: LinkService = Depends(get_link_service)):
    return {"tasks": service.list_tasks(
        current.session,
        status=request.query_params.get("status", ""),
        limit=_int_param(request, "limit", 50, low=1, high=200))}


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
    after = _int_param(request, "after", 0, low=0, high=10 ** 9)
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
    since = _int_param(request, "since", 0, low=0, high=10 ** 9)
    focus = request.query_params.get("task", "")[:64]
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
