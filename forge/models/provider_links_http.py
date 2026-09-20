"""Authenticated public AI-provider resource links for Forge clients."""
from __future__ import annotations

from typing import Any, Dict

from starlette.requests import Request

from forge.models.provider_links import enrich_provider_info, provider_link_catalog


def install_provider_links_route(app: Any, *, prefix: str = "/api/v1", require: Any = None) -> None:
    """Mount GET /provider-links using the existing models.status permission."""
    if require is None:
        raise ValueError("require callback is required")

    @app.get(prefix.rstrip("/") + "/provider-links")
    #: ``Request`` (not ``Any``): a bare ``Any`` annotation is interpreted by
    #: FastAPI as a required query parameter, which broke the endpoint.
    #: ``Dict[str, Any]`` (not ``dict[str, Any]``): FastAPI evaluates the
    #: return annotation when it registers the route, and builtin subscripting
    #: is a TypeError on Python 3.8 — the future import does not help, because
    #: the string is evaluated by FastAPI rather than by the interpreter.
    def provider_links(request: Request) -> Dict[str, Any]:
        require(request, "models.status")
        return {"schema_version": 1, "providers": provider_link_catalog()}


def enrich(info: Dict[str, Any]) -> Dict[str, Any]:
    """Convenience adapter for provider/model UI records."""
    return enrich_provider_info(info)


__all__ = ["install_provider_links_route", "enrich"]
