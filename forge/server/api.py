"""The Forge Server HTTP API gateway (A81).

A thin, strict translation layer over :class:`~forge.server.server.ForgeServer`:

* **Authenticated**: every route (except the anonymous ``/ping``
  liveness probe) requires a bearer token — API key, session token, or
  bootstrap token. Unknown/expired/revoked credentials fail closed 401.
* **Authorized**: each route maps to exactly one operation in the
  closed :data:`~forge.server.authorization.API_OPERATIONS` table and
  requires its scope; missing scopes fail closed 403.
* **Validated**: request schemas forbid unknown fields, bound every
  string and integer, and reject execution-shaped payloads. There is no
  route — and no field — that accepts a command to run: the API is a
  task backend, never a remote shell.
* **Structured errors**: ``{"error": {"code", "message", "request_id"}}``
  with stable codes; no tracebacks, secrets, or internal paths escape.

Endpoints (all under ``/api/v1``): task creation/status/list, pause,
resume, cancel, retry, rollback, logs, result, events (with long-poll
``wait``), approvals list/decide, project registration/list, sessions,
API keys, notifications, recovery (reconnect bundle), health, status,
and a read-only policy view.

Live updates are pull-based long polls (``GET .../events?after=<seq>&
wait=<s>``) over the persistent event log — durable across restarts,
exact replay from any cursor, and friendly to any HTTP client (no
websocket stack required; Python 3.8 / Windows safe).
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from forge.server.authorization import reject_execution_vectors
from forge.server.errors import (
    AuthenticationRequired,
    InvalidRequest,
    RateLimited,
    ServerError,
)
from forge.server.models import TaskStatus

#: API version prefix.
API_PREFIX = "/api/v1"

#: Long-poll ceiling for ``wait`` (seconds).
MAX_WAIT_SECONDS = 25.0


# -- strict request schemas ---------------------------------------------------

class _Strict(BaseModel):
    """Unknown fields are rejected, never ignored (remote-shell guard)."""

    model_config = ConfigDict(extra="forbid")


class TaskCreateRequest(_Strict):
    project_id: str = Field(min_length=1, max_length=128)
    requirement: str = Field(min_length=1, max_length=8000)
    priority: int = Field(default=0, ge=-1000, le=1000)
    mode: str = Field(default="", max_length=32)
    max_retries: Optional[int] = Field(default=None, ge=0, le=10)


class TaskActionRequest(_Strict):
    expected_version: Optional[int] = Field(default=None, ge=1)


class ProjectRegisterRequest(_Strict):
    project_id: str = Field(min_length=1, max_length=128)
    root: str = Field(min_length=1, max_length=4096)
    name: str = Field(default="", max_length=128)


class ApprovalDecideRequest(_Strict):
    approved: bool


class SessionCreateRequest(_Strict):
    """Optional challenge binding for a session exchange.

    A thin client (G560) always sends a fresh challenge nonce so the
    exchange is replay-resistant: a captured ``POST /auth/sessions``
    cannot be re-presented because its nonce was consumed (or expired).
    """
    challenge_nonce: Optional[str] = Field(default=None, max_length=128)


class KeyCreateRequest(_Strict):
    name: str = Field(min_length=1, max_length=64)
    role: str = Field(default="operator", max_length=32)


class NotificationsReadAllRequest(_Strict):
    project_id: str = Field(default="", max_length=128)


# -- error rendering -------------------------------------------------------------

def error_body(code: str, message: str, request_id: str,
               details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "error": {"code": code, "message": message,
                  "request_id": request_id},
    }
    if details:
        # Details are server-generated values (ids, versions, statuses) —
        # never raw exceptions or filesystem paths.
        body["error"]["details"] = {
            key: value for key, value in details.items()
            if isinstance(value, (str, int, float, bool)) or value is None}
    return body


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or "")


async def _server_error(request: Request, exc: ServerError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content=error_body(exc.code, str(exc) or exc.code,
                           _request_id(request), exc.details or None))


async def _http_error(request: Request,
                      exc: StarletteHTTPException) -> JSONResponse:
    code = "NOT_FOUND" if exc.status_code == 404 else "INVALID_REQUEST"
    message = ("Not found." if exc.status_code == 404 else
               (str(exc.detail) if isinstance(exc.detail, str)
                else "Request failed."))
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(code, message, _request_id(request)))


async def _validation_error(request: Request,
                            exc: RequestValidationError) -> JSONResponse:
    # Schema-driven messages only; input values are never echoed.
    problems = []
    for item in exc.errors():
        location = ".".join(str(part) for part in item.get("loc", ()))
        problems.append("%s: %s" % (location, item.get("msg", "invalid")))
    return JSONResponse(
        status_code=400,
        content=error_body("INVALID_REQUEST",
                           "; ".join(problems[:5]) or "Invalid request.",
                           _request_id(request)))


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    del exc  # never reflected to the client
    return JSONResponse(
        status_code=500,
        content=error_body("INTERNAL_ERROR",
                           "An unexpected error occurred.",
                           _request_id(request)))


def install_handlers(app: FastAPI) -> None:
    app.add_exception_handler(
        ServerError, _server_error)  # type: ignore[arg-type]
    app.add_exception_handler(
        StarletteHTTPException, _http_error)  # type: ignore[arg-type]
    app.add_exception_handler(
        RequestValidationError, _validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled)


# -- middleware ---------------------------------------------------------------------

class _RequestContextMiddleware(BaseHTTPMiddleware):
    """Request ids plus safe response headers."""

    async def dispatch(self, request: Request, call_next):
        # The auth middleware may already have stamped a request id.
        request_id = (getattr(request.state, "request_id", "")
                      or request.headers.get("x-request-id") or uuid4().hex)
        request.state.request_id = request_id[:64]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response


class _AuthMiddleware(BaseHTTPMiddleware):
    """Authenticate before anything else — bodies are never parsed first.

    Every ``/api/v1`` route except the anonymous liveness probe requires
    a bearer token. Running authentication in middleware (outside the
    router) means an unauthenticated request can never probe schema
    validation: it always receives 401, whatever the body looks like.
    """

    def __init__(self, app: Any, server: Any) -> None:
        super().__init__(app)
        self.server = server

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (path == API_PREFIX + "/ping"
                or path == API_PREFIX + "/auth/challenge"
                or not path.startswith(API_PREFIX)):
            # /auth/challenge is anonymous on purpose: it issues a
            # single-use, time-bounded nonce that reveals nothing and
            # is useless without the credential presented on the
            # (authenticated) session exchange that follows it.
            return await call_next(request)
        request.state.request_id = (
            request.headers.get("x-request-id") or uuid4().hex)[:64]
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return self._reject(
                request, AuthenticationRequired(
                    "Authentication required: send "
                    "'Authorization: Bearer <token>'."))
        try:
            principal = self.server.authenticate(
                header.split(" ", 1)[1].strip())
        except ServerError as exc:
            return self._reject(request, exc)
        request.state.principal = principal
        return await call_next(request)

    @staticmethod
    def _reject(request: Request, exc: ServerError) -> JSONResponse:
        response = JSONResponse(
            status_code=exc.status,
            content=error_body(exc.code, str(exc) or exc.code,
                               str(getattr(request.state, "request_id", "")),
                               exc.details or None))
        response.headers["X-Request-ID"] = str(
            getattr(request.state, "request_id", ""))
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response


class _BodyLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized request bodies by declared length."""

    def __init__(self, app: Any, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        length = request.headers.get("content-length")
        if length is not None:
            try:
                if int(length) > self.max_bytes:
                    return JSONResponse(
                        status_code=413,
                        content=error_body(
                            "INVALID_REQUEST", "Request body too large.",
                            str(getattr(request.state, "request_id", ""))))
            except ValueError:
                pass
        return await call_next(request)


# -- rate limiting ---------------------------------------------------------------------

class TokenBucketLimiter:
    """Bounded in-memory token buckets (per principal, per operation)."""

    def __init__(self, rate_per_minute: float = 60.0, burst: int = 20,
                 max_keys: int = 1024) -> None:
        self.rate_per_second = max(0.001, rate_per_minute / 60.0)
        self.burst = max(1, int(burst))
        self.max_keys = max_keys
        self._buckets: "OrderedDict[str, List[float]]" = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            entry = self._buckets.get(key)
            if entry is None:
                self._buckets[key] = [float(self.burst - 1), now]
            else:
                tokens, updated = entry
                tokens = min(float(self.burst),
                             tokens + (now - updated) * self.rate_per_second)
                if tokens < 1.0:
                    entry[1] = now
                    self._buckets.move_to_end(key)
                    return False
                entry[0] = tokens - 1.0
                entry[1] = now
            self._buckets.move_to_end(key)
            while len(self._buckets) > self.max_keys:
                self._buckets.popitem(last=False)
            return True


# -- route helpers ---------------------------------------------------------------------

def _server(request: Request) -> Any:
    return request.app.state.server


def _principal(request: Request) -> Any:
    """The middleware-authenticated principal (fail closed if absent)."""
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise AuthenticationRequired("Authentication required.")
    return principal


def _require(request: Request, operation: str) -> Any:
    """Authenticate + authorize one closed API operation."""
    principal = _principal(request)
    request.app.state.server.authorizer.require(principal, operation)
    return principal


def _limit(request: Request, principal: Any, bucket: str) -> None:
    limiter = request.app.state.limiter
    if not limiter.allow("%s:%s" % (bucket, principal.name)):
        raise RateLimited("Rate limit exceeded; retry shortly.")


def _query_int(request: Request, name: str, default: int, *,
               low: int = 0, high: int = 10 ** 9) -> int:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise InvalidRequest("Query parameter %r must be an integer."
                             % name) from None
    return max(low, min(high, value))


def _query_float(request: Request, name: str, default: float, *,
                 low: float = 0.0, high: float = 10 ** 9) -> float:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise InvalidRequest("Query parameter %r must be a number."
                             % name) from None
    return max(low, min(high, value))


def _query_str(request: Request, name: str, default: str = "") -> str:
    return str(request.query_params.get(name, default) or default)[:128]


# -- application factory ---------------------------------------------------------------------

def create_app(server: Any, *,
               max_body_bytes: int = 1024 * 1024) -> FastAPI:
    """Build the FastAPI application bound to one :class:`ForgeServer`."""

    app = FastAPI(
        title="Forge Server",
        version=_version(),
        # Closed surface: no interactive docs, no OpenAPI schema served.
        docs_url=None, redoc_url=None, openapi_url=None)
    app.state.server = server
    app.state.limiter = TokenBucketLimiter(rate_per_minute=120.0, burst=30)

    # Middleware order (outermost first): CORS → auth → body limit →
    # request context. Authentication runs before bodies are parsed.
    app.add_middleware(_RequestContextMiddleware)
    app.add_middleware(_BodyLimitMiddleware, max_bytes=max_body_bytes)
    app.add_middleware(_AuthMiddleware, server=server)
    if server.config.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(server.config.allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"])
    install_handlers(app)
    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:  # noqa: C901 - flat route table
    prefix = API_PREFIX

    # -- liveness (anonymous; leaks nothing) ----------------------------------

    @app.get(prefix + "/ping")
    def ping() -> Dict[str, Any]:
        return {"ok": True, "server": "forge-server"}

    # -- auth: identity, sessions, API keys -------------------------------------

    @app.get(prefix + "/auth/whoami")
    def whoami(request: Request) -> Dict[str, Any]:
        principal = _principal(request)
        return {"principal": principal.to_dict()}

    @app.get(prefix + "/auth/challenge")
    def auth_challenge(request: Request) -> Dict[str, Any]:
        # Anonymous by design: the challenge is a single-use nonce that
        # binds a subsequent session exchange; it reveals nothing and
        # cannot be used without the credential that follows it.
        # Rate-limited by client address (no principal yet).
        client_host = (request.client.host if request.client else "?")
        if not request.app.state.limiter.allow(
                "challenges:%s" % client_host):
            raise RateLimited("Rate limit exceeded; retry shortly.")
        backend = _server(request)
        return backend.auth.new_challenge()

    @app.post(prefix + "/auth/sessions")
    def create_session(request: Request,
                       payload: Optional[SessionCreateRequest] = None
                       ) -> Dict[str, Any]:
        # Any authenticated principal may open a session for *itself*;
        # the session inherits role and scopes, so it can never escalate.
        principal = _principal(request)
        _limit(request, principal, "sessions")
        backend = _server(request)
        nonce = (payload.challenge_nonce if payload is not None else None)
        if nonce is not None:
            # A supplied challenge must be fresh and unused; consume it.
            if not backend.auth.consume_challenge(nonce):
                raise InvalidRequest(
                    "Challenge is unknown, expired, or already used; "
                    "request a fresh one from /auth/challenge.")
        elif backend.config.require_challenge:
            raise InvalidRequest(
                "This server requires a challenge nonce for session "
                "creation (POST /auth/challenge first).")
        session, token = backend.sessions.create(
            principal.name, principal.role, sorted(principal.scopes),
            metadata={"via": principal.via,
                      "challenge": bool(nonce)})
        return {"session": session.to_dict(), "token": token}

    @app.get(prefix + "/auth/sessions")
    def list_sessions(request: Request) -> Dict[str, Any]:
        _require(request, "session.list")
        backend = _server(request)
        return {"sessions": [session.to_dict()
                             for session in backend.sessions.list_active()]}

    @app.delete(prefix + "/auth/sessions/{session_id}")
    def revoke_session(request: Request, session_id: str) -> Dict[str, Any]:
        principal = _require(request, "session.revoke")
        backend = _server(request)
        session = backend.sessions.get(session_id)
        if session is None:
            raise InvalidRequest("Unknown session.", session_id=session_id)
        own = (session.principal == principal.name
               and session.id == principal.session_id)
        if not own and "sessions:write" not in principal.scopes:
            raise InvalidRequest("Cannot revoke another principal's "
                                 "session.", session_id=session_id)
        revoked = backend.sessions.revoke(session_id)
        return {"revoked": revoked, "session_id": session_id}

    @app.post(prefix + "/auth/keys")
    def create_key(payload: KeyCreateRequest,
                   request: Request) -> Dict[str, Any]:
        principal = _require(request, "key.create")
        _limit(request, principal, "keys")
        backend = _server(request)
        reject_execution_vectors(payload.model_dump())
        record = backend.auth.create_api_key(payload.name, payload.role)
        return {"key": record}

    @app.get(prefix + "/auth/keys")
    def list_keys(request: Request) -> Dict[str, Any]:
        _require(request, "key.create")
        backend = _server(request)
        return {"keys": backend.auth.list_api_keys()}

    @app.delete(prefix + "/auth/keys/{name}")
    def revoke_key(request: Request, name: str) -> Dict[str, Any]:
        _require(request, "key.revoke")
        backend = _server(request)
        return {"revoked": backend.auth.revoke_api_key(name), "name": name}

    # -- projects ------------------------------------------------------------------

    @app.post(prefix + "/projects")
    def register_project(payload: ProjectRegisterRequest,
                         request: Request) -> Dict[str, Any]:
        principal = _require(request, "project.register")
        _limit(request, principal, "projects")
        backend = _server(request)
        reject_execution_vectors(payload.model_dump())
        project = backend.projects.register(
            payload.project_id, payload.root, name=payload.name)
        backend.emit("", project.project_id, "project.registered",
                     {"actor": principal.name, "root": project.root})
        return {"project": project.to_dict()}

    @app.get(prefix + "/projects")
    def list_projects(request: Request) -> Dict[str, Any]:
        _require(request, "project.list")
        backend = _server(request)
        return {"projects": [project.to_dict()
                             for project in backend.projects.list()]}

    # -- tasks --------------------------------------------------------------------------

    @app.post(prefix + "/tasks")
    def create_task(payload: TaskCreateRequest,
                    request: Request) -> Dict[str, Any]:
        principal = _require(request, "task.create")
        _limit(request, principal, "tasks")
        backend = _server(request)
        # Defense in depth: the closed schema already forbids unknown
        # fields; this guard makes the no-remote-shell rule explicit.
        reject_execution_vectors(payload.model_dump())
        task = backend.submit_task(
            payload.project_id, payload.requirement,
            priority=payload.priority, mode=payload.mode,
            actor=principal.name, max_retries=payload.max_retries)
        return {"task": task.to_dict()}

    @app.get(prefix + "/tasks")
    def list_tasks(request: Request) -> Dict[str, Any]:
        _require(request, "task.list")
        backend = _server(request)
        project_id = _query_str(request, "project_id")
        status = _query_str(request, "status")
        limit = _query_int(request, "limit", 50, low=1, high=500)
        if status:
            try:
                status = TaskStatus(status).value
            except ValueError:
                raise InvalidRequest(
                    "Unknown status %r; want one of: %s."
                    % (status, ", ".join(s.value for s in TaskStatus))) \
                    from None
        tasks = backend.tasks.list(
            project_id=project_id, status=status, limit=limit)
        return {"tasks": [task.to_dict() for task in tasks],
                "counts": backend.tasks.status_counts(project_id)}

    @app.get(prefix + "/tasks/{task_id}")
    def get_task(request: Request, task_id: str) -> Dict[str, Any]:
        _require(request, "task.read")
        backend = _server(request)
        task = backend.tasks.get_or_raise(task_id)
        payload = task.to_dict(include_result=task.terminal)
        payload["queue_position"] = backend.queue.position(task_id)
        return {"task": payload}

    @app.post(prefix + "/tasks/{task_id}/pause")
    def pause_task(payload: TaskActionRequest, request: Request,
                   task_id: str) -> Dict[str, Any]:
        principal = _require(request, "task.pause")
        backend = _server(request)
        task = backend.pause_task(
            task_id, actor=principal.name,
            expected_version=payload.expected_version)
        return {"task": task.to_dict()}

    @app.post(prefix + "/tasks/{task_id}/resume")
    def resume_task(payload: TaskActionRequest, request: Request,
                    task_id: str) -> Dict[str, Any]:
        principal = _require(request, "task.resume")
        backend = _server(request)
        task = backend.resume_task(
            task_id, actor=principal.name,
            expected_version=payload.expected_version)
        return {"task": task.to_dict()}

    @app.post(prefix + "/tasks/{task_id}/cancel")
    def cancel_task(payload: TaskActionRequest, request: Request,
                    task_id: str) -> Dict[str, Any]:
        principal = _require(request, "task.cancel")
        backend = _server(request)
        task = backend.cancel_task(
            task_id, actor=principal.name,
            expected_version=payload.expected_version)
        return {"task": task.to_dict()}

    @app.post(prefix + "/tasks/{task_id}/retry")
    def retry_task(request: Request, task_id: str) -> Dict[str, Any]:
        principal = _require(request, "task.retry")
        backend = _server(request)
        task = backend.retry_task(task_id, actor=principal.name)
        return {"task": task.to_dict()}

    @app.post(prefix + "/tasks/{task_id}/rollback")
    def rollback_task(payload: TaskActionRequest, request: Request,
                      task_id: str) -> Dict[str, Any]:
        principal = _require(request, "task.rollback")
        backend = _server(request)
        task = backend.rollback_task(
            task_id, actor=principal.name,
            expected_version=payload.expected_version)
        return {"task": task.to_dict()}

    @app.get(prefix + "/tasks/{task_id}/logs")
    def task_logs(request: Request, task_id: str) -> Dict[str, Any]:
        _require(request, "task.logs")
        backend = _server(request)
        backend.tasks.get_or_raise(task_id)
        after = _query_int(request, "after", 0, low=0)
        limit = _query_int(request, "limit", 200, low=1, high=1000)
        level = _query_str(request, "level")
        entries, latest = backend.logs.list(
            task_id, after=after, level=level, limit=limit)
        return {"task_id": task_id,
                "logs": [entry.to_dict() for entry in entries],
                "latest_id": latest}

    @app.get(prefix + "/tasks/{task_id}/result")
    def task_result(request: Request, task_id: str) -> Dict[str, Any]:
        _require(request, "task.result")
        backend = _server(request)
        task = backend.tasks.get_or_raise(task_id)
        return {"task_id": task_id, "status": task.status.value,
                "error": task.error,
                "result": task.result() if task.terminal else {},
                "available": task.terminal}

    @app.get(prefix + "/tasks/{task_id}/events")
    def task_events(request: Request, task_id: str) -> Dict[str, Any]:
        _require(request, "task.events")
        backend = _server(request)
        backend.tasks.get_or_raise(task_id)
        after = _query_int(request, "after", 0, low=0)
        limit = _query_int(request, "limit", 200, low=1, high=500)
        wait = _query_float(request, "wait", 0.0,
                            low=0.0, high=MAX_WAIT_SECONDS)
        if wait > 0:
            events = backend.events.wait(task_id, after, timeout=wait)
            latest = backend.events.latest_seq(task_id)
        else:
            events, latest = backend.events.list(
                task_id, after=after, limit=limit)
        return {"task_id": task_id, "latest_seq": latest,
                "events": [event.to_dict() for event in events]}

    # -- approvals --------------------------------------------------------------------------

    @app.get(prefix + "/approvals")
    def list_approvals(request: Request) -> Dict[str, Any]:
        _require(request, "approval.list")
        backend = _server(request)
        project_id = _query_str(request, "project_id")
        return {"approvals": backend.approvals.pending(project_id)}

    @app.get(prefix + "/tasks/{task_id}/approvals")
    def task_approvals(request: Request, task_id: str) -> Dict[str, Any]:
        _require(request, "approval.list")
        backend = _server(request)
        backend.tasks.get_or_raise(task_id)
        return {"task_id": task_id,
                "approvals": backend.approvals.list_for_task(task_id)}

    @app.post(prefix + "/approvals/{approval_id}/decide")
    def decide_approval(payload: ApprovalDecideRequest,
                        request: Request,
                        approval_id: str) -> Dict[str, Any]:
        principal = _require(request, "approval.decide")
        _limit(request, principal, "approvals")
        backend = _server(request)
        record = backend.decide_approval(
            approval_id, bool(payload.approved), principal.name)
        return {"approval": record}

    # -- notifications ------------------------------------------------------------------------

    @app.get(prefix + "/notifications")
    def list_notifications(request: Request) -> Dict[str, Any]:
        _require(request, "notifications.list")
        backend = _server(request)
        project_id = _query_str(request, "project_id")
        unread = _query_str(request, "unread", "1") not in ("0", "false", "")
        limit = _query_int(request, "limit", 50, low=1, high=200)
        return {"notifications": backend.notifications.list(
            project_id, unread_only=unread, limit=limit)}

    @app.post(prefix + "/notifications/{notification_id}/read")
    def mark_notification_read(request: Request,
                               notification_id: str) -> Dict[str, Any]:
        _require(request, "notifications.read")
        backend = _server(request)
        return {"marked": backend.notifications.mark_read(notification_id),
                "notification_id": notification_id}

    @app.post(prefix + "/notifications/read-all")
    def mark_notifications_read(
            payload: NotificationsReadAllRequest,
            request: Request) -> Dict[str, Any]:
        _require(request, "notifications.read")
        backend = _server(request)
        return {"marked": backend.notifications.mark_all_read(
            payload.project_id)}

    # -- operations: recovery, health, status, policy ---------------------------------------------

    @app.get(prefix + "/recovery")
    def recovery(request: Request) -> Dict[str, Any]:
        _require(request, "recovery.read")
        backend = _server(request)
        after = _query_int(request, "after", 0, low=0)
        since_raw = request.query_params.get("since")
        since: Optional[float] = None
        if since_raw not in (None, ""):
            # Epoch timestamps need headroom beyond the generic bound.
            since = _query_float(request, "since", 0.0, low=0.0,
                                 high=10.0 ** 11)
        project_id = _query_str(request, "project_id")
        return backend.recovery_bundle(
            after=after, since=since, project_id=project_id)

    @app.get(prefix + "/health")
    def health(request: Request) -> Dict[str, Any]:
        _require(request, "health.read")
        return _server(request).health()

    @app.get(prefix + "/status")
    def status(request: Request) -> Dict[str, Any]:
        _require(request, "status.read")
        return _server(request).status()

    @app.get(prefix + "/policy")
    def policy_view(request: Request) -> Dict[str, Any]:
        _require(request, "task.read")
        backend = _server(request)
        return {"policy": backend.authorizer.describe()}


def _version() -> str:
    try:
        from forge.server import SERVER_VERSION

        return SERVER_VERSION
    except Exception:
        return "0.0.0"
