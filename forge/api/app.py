"""FastAPI application factory for the cockpit API (A34)."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import List, Optional, Union
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from forge.api import (
    routes_approvals, routes_assistant, routes_channels, routes_commands,
    routes_core, routes_memory, routes_multimodal,
    routes_tasks, routes_views, stream, routes_orchestrations,
    routes_vision, routes_computer, routes_agents, routes_voice_conversation,
    routes_conversation, routes_collaboration, routes_council, routes_models,
    routes_research, routes_compute, routes_teams, routes_skills,
    routes_learning, routes_benchmarks, routes_hardening, routes_observability,
    routes_performance, routes_deployments, routes_backups, routes_plugins,
    routes_autonomy, routes_final, routes_staged, routes_engine,
    routes_milestones, routes_runtimes, routes_workers, routes_readiness,
)
from forge.api.deps import RateLimiter
from forge.api.errors import error_body, install_handlers
from forge.control.control_plane import ControlPlane
from forge.workers.heartbeat_service import WorkerHeartbeatService
from forge.workers.persistence import WorkerStore
from forge.workers.registry import WorkerRegistry


@dataclass
class ApiConfig:
    allowed_origins: List[str] = field(default_factory=list)
    secure_cookies: bool = False
    max_body_bytes: int = 1024 * 1024
    web_dir: Optional[Union[str, Path]] = None


class _RequestContextMiddleware(BaseHTTPMiddleware):
    """Request ids plus safe response headers."""
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid4().hex
        request.state.request_id = request_id[:64]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; "
                "media-src 'self' blob:; base-uri 'self'; form-action 'self'")
        if not request.scope.get("path", "").startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response


def _release_service(current, previous, created):
    """Pick the value to leave attached to the plane after a teardown.

    Lifespans can overlap (a client entered twice, an in-process restart,
    a reload), so teardown order is not guaranteed. Rules:

    * a newer live service stays attached — it owns the plane now;
    * otherwise the value captured on entry is restored;
    * a *stopped* service is never left attached, because a caller would
      then schedule work onto a dead heartbeat/monitor.
    """
    value = current
    if current is created or current is None \
            or not getattr(current, "running", False):
        value = previous
    if value is not None and not getattr(value, "running", False):
        value = None
    return value


class _BodyLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized request bodies before they are read."""
    def __init__(self, app, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        length = request.headers.get("content-length")
        if length is not None:
            try:
                if int(length) > self.max_bytes:
                    return JSONResponse(
                        status_code=413,
                        content=error_body("INVALID_REQUEST",
                                           "Request body too large.",
                                           str(getattr(request.state, "request_id", ""))))
            except ValueError:
                pass
        read = 0
        receive = request._receive
        async def bounded_receive():
            nonlocal read
            message = await receive()
            if message["type"] == "http.request":
                read += len(message.get("body", b""))
                if read > self.max_bytes:
                    return {"type": "http.disconnect"}
            return message
        request._receive = bounded_receive
        try:
            return await call_next(request)
        finally:
            request._receive = receive


def served_route_paths(app: FastAPI) -> List[str]:
    """Return every path the app actually serves, newest FastAPI included.

    FastAPI >= 0.141 includes sub-routers lazily (``_IncludedRouter``), so
    ``app.routes`` no longer flattens to ``APIRoute`` objects and naive
    introspection silently reports nothing. The OpenAPI schema is generated
    from the effective routing table, so it reflects the served surface on
    every supported version. Never raises: an app whose schema cannot be
    built reports the paths it does expose directly.
    """
    paths: List[str] = []
    for route in getattr(app, "routes", []) or []:
        path = str(getattr(route, "path", "") or "")
        if path:
            paths.append(path)
        contexts = getattr(route, "effective_route_contexts", None)
        if callable(contexts):
            try:
                for context in contexts() or []:
                    candidate = getattr(context, "path", "") or \
                        getattr(getattr(context, "route", None), "path", "")
                    if candidate:
                        paths.append(str(candidate))
            except Exception:
                pass
    try:
        schema = app.openapi()
        paths.extend(str(path) for path in (schema.get("paths") or {}))
    except Exception:
        pass
    return sorted(set(paths))


def create_app(plane: ControlPlane,
               config: Optional[ApiConfig] = None) -> FastAPI:
    """Build the cockpit API over an existing control plane."""
    config = config or ApiConfig()
    if "*" in config.allowed_origins:
        raise ValueError('allow_origins=["*"] is never permitted; list explicit origins')

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        plane = app.state.plane
        from forge.control.approval_persistence import restore_approval_requests
        restore_approval_requests(plane)
        from forge.models.runtime_monitor_service import RuntimeMonitorService
        monitor_path = Path(plane.config.db_path).parent / "runtime-monitor.json"
        #: Capture what was already there so a nested/repeated lifespan
        #: (nested ``with`` blocks, in-process restart, uvicorn reload) is a
        #: stack push/pop instead of a destructive overwrite.
        previous_monitor = getattr(plane, "runtime_monitor", None)
        monitor = RuntimeMonitorService(plane.fabric, state_path=monitor_path)
        plane.runtime_monitor = monitor
        monitor.start()

        registry = plane.worker_registry
        store = WorkerStore(
            str(Path(plane.config.db_path).parent / "workers.db"))
        previous_store = getattr(plane, "worker_store", None)
        plane.worker_store = store
        store.restore(registry)
        previous_heartbeat = getattr(plane, "worker_heartbeat_service", None)
        heartbeat_service = WorkerHeartbeatService(registry)
        plane.worker_heartbeat_service = heartbeat_service
        heartbeat_service.start()

        # Only the lifespan that started the pool stops it.
        started_pool = not plane.running
        plane.start()
        try:
            from forge.staged.autorun import resume_active
            resume_active(plane)
            yield
        finally:
            heartbeat_service.stop(wait=True)
            monitor.stop(wait=True)
            #: Restore the value captured on entry. Overlapping lifespans
            #: (a client entered twice, an in-process restart) can interleave
            #: their teardowns, so a service that is no longer running is
            #: never left attached to the plane as if it were live.
            plane.runtime_monitor = _release_service(
                getattr(plane, "runtime_monitor", None), previous_monitor,
                monitor)
            plane.worker_heartbeat_service = _release_service(
                getattr(plane, "worker_heartbeat_service", None),
                previous_heartbeat, heartbeat_service)
            if getattr(plane, "worker_store", None) is store:
                plane.worker_store = previous_store
            if started_pool:
                plane.stop(wait=False)

    app = FastAPI(title="Forge Cockpit API", version="1.0.0",
                  docs_url="/api/docs", redoc_url="/api/redoc",
                  openapi_url="/api/openapi.json", lifespan=lifespan)
    app.state.plane = plane
    app.state.limiter = RateLimiter()
    app.state.secure_cookies = config.secure_cookies
    if getattr(plane, "worker_registry", None) is None:
        plane.worker_registry = WorkerRegistry()

    if config.allowed_origins:
        app.add_middleware(CORSMiddleware,
                           allow_origins=list(config.allowed_origins),
                           allow_credentials=True,
                           allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
                           allow_headers=["Authorization", "Content-Type",
                                          "X-Requested-With", "X-Request-ID"],
                           max_age=600)
    app.add_middleware(_BodyLimitMiddleware, max_bytes=config.max_body_bytes)
    app.add_middleware(_RequestContextMiddleware)
    install_handlers(app)

    for router in (
        routes_core.router, routes_tasks.router, routes_approvals.router,
        routes_views.router, routes_commands.router, routes_memory.router,
        routes_orchestrations.router, routes_vision.router, routes_computer.router,
        routes_agents.router, routes_voice_conversation.router,
        routes_conversation.router, routes_collaboration.router,
        routes_council.router, routes_models.router, routes_research.router,
        routes_teams.router, routes_skills.router, routes_learning.router,
        routes_benchmarks.router, routes_hardening.router,
        routes_observability.router, routes_performance.router,
        routes_deployments.router, routes_backups.router, routes_plugins.router,
        routes_autonomy.router, routes_final.router, routes_staged.router,
        routes_compute.router, routes_engine.router, routes_milestones.router,
        routes_runtimes.router, routes_workers.router, routes_readiness.router,
        routes_channels.router, routes_multimodal.router,
        routes_assistant.router,
        stream.router,
    ):
        app.include_router(router, prefix="/api/v1")

    web_dir = (Path(config.web_dir) if config.web_dir else
               Path(__file__).resolve().parent.parent / "cockpit" / "web")
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="cockpit")
    return app
