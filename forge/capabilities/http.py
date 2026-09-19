"""Authenticated capability-truth route installer.

The installer is deliberately separate from the server gateway so the same
truth contract can be mounted by the Forge Server and desktop/cockpit APIs
without duplicating capability logic.  It reuses the existing closed
``models.status`` authorization operation rather than introducing a second
permission vocabulary.
"""
from __future__ import annotations

from typing import Any, Dict

from starlette.requests import Request

from forge.capabilities.runtime import runtime_capability_snapshot


def install_capability_route(app: Any, *, prefix: str = "/api/v1",
                             require: Any = None,
                             server_getter: Any = None) -> None:
    """Mount ``GET /capabilities`` on an existing FastAPI app.

    ``require`` must be the host gateway's authorization helper and is called
    with ``models.status``.  This means the route cannot accidentally become
    anonymous when mounted into the authenticated Forge Server.
    """
    if require is None or server_getter is None:
        raise ValueError("require and server_getter callbacks are required")

    @app.get(prefix.rstrip("/") + "/capabilities")
    #: ``Request`` (not ``Any``) matters: annotating the parameter as ``Any``
    #: makes FastAPI treat it as a *required query parameter* named
    #: ``request``, so the route answered 400 to every real caller.
    def capabilities(request: Request) -> Dict[str, Any]:
        require(request, "models.status")
        server = server_getter(request)
        fabric = getattr(server, "fabric", None)
        if fabric is None:
            inference = getattr(server, "inference", None)
            fabric = getattr(inference, "fabric", None)
        return runtime_capability_snapshot(fabric)


__all__ = ["install_capability_route"]
