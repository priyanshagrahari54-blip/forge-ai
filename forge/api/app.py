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
    routes_approvals, routes_commands, routes_core, routes_memory,
    routes_tasks, routes_views, stream, routes_orchestrations,
    routes_vision, routes_computer, routes_agents, routes_voice_conversation,
    routes_conversation, routes_collaboration, routes_council, routes_models,
    routes_research, routes_compute, routes_teams, routes_skills,
    routes_learning, routes_benchmarks, routes_hardening, routes_observability,
    routes_performance, routes_deployments, routes_backups, routes_plugins,
    routes_autonomy, routes_final, routes_staged, routes_engine,
    routes_milestones, routes_runtimes, routes_workers,
)
from forge.api.deps import RateLimiter
from forge.api.errors import error_body, install_handlers
from forge.control.control_plane import ControlPlane
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


def create_app(plane: ControlPlane,
               config: Optional[ApiConfig] = None) -> FastAPI:
    """Build the cockpit API over an existing control plane."""
    config = config or ApiConfig()
    if "*" in config.allowed_origins:
        raise ValueError('allow_origins=["*"] is never permitted; list explicit origins')

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from forge.control.approval_persistence import restore_approval_requests
        restore_approval_requests(app.state.plane)
        from forge.models.runtime_monitor_service import RuntimeMonitorService
        monitor_path = Path(app.state.plane.config.db_path).parent / "runtime-monitor.json"
        app.state.plane.runtime_monitor = RuntimeMonitorService(
            app.state.plane.fabric,
            state_path=monitor_path,
        )
        app.state.plane.runtime_monitor.start()
        app.state.plane.start()
        try:
            from forge.staged.autorun import resume_active
            resume_active(app.state.plane)
            yield
        finally:
            app.state.plane.runtime_monitor.stop(wait=True)
            app.state.plane.stop(wait=False)
            app.state.plane.runtime_monitor = None

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
        routes_runtimes.router, routes_workers.router, stream.router,
    ):
        app.include_router(router, prefix="/api/v1")

    web_dir = (Path(config.web_dir) if config.web_dir else
               Path(__file__).resolve().parent.parent / "cockpit" / "web")
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="cockpit")
    return app
