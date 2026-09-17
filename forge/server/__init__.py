"""Forge Server (A81): the standalone task backend for Forge AI."""
from forge.server.models import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    ProjectInfo,
    ServerTask,
    TaskStatus,
)
from forge.server.server import PROTOCOL_VERSION, ForgeServer, ServerConfig

SERVER_VERSION = "0.1.0"

from forge.capabilities.http import install_capability_route  # noqa: E402
from forge.models.provider_links_http import install_provider_links_route  # noqa: E402

_original_create_app = ForgeServer.create_app


def _create_app_with_capabilities(self):
    app = _original_create_app(self)
    require = lambda request, operation: app.state.server.authorizer.require(
        getattr(request.state, "principal", None), operation
    )
    install_capability_route(
        app,
        require=require,
        server_getter=lambda request: request.app.state.server,
    )
    install_provider_links_route(app, require=require)
    return app


ForgeServer.create_app = _create_app_with_capabilities

__all__ = [
    "ACTIVE_STATUSES",
    "TERMINAL_STATUSES",
    "ForgeServer",
    "PROTOCOL_VERSION",
    "ProjectInfo",
    "SERVER_VERSION",
    "ServerConfig",
    "ServerTask",
    "TaskStatus",
]
