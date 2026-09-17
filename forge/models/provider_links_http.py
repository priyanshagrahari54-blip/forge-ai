"""Authenticated public AI-provider resource links for Forge clients."""
from __future__ import annotations

from typing import Any

from forge.models.provider_links import enrich_provider_info, provider_link_catalog


def install_provider_links_route(app: Any, *, prefix: str = "/api/v1", require: Any = None) -> None:
    """Mount GET /provider-links using the existing models.status permission."""
    if require is None:
        raise ValueError("require callback is required")

    @app.get(prefix.rstrip("/") + "/provider-links")
    def provider_links(request: Any) -> dict[str, Any]:
        require(request, "models.status")
        return {"schema_version": 1, "providers": provider_link_catalog()}


def enrich(info: dict[str, Any]) -> dict[str, Any]:
    """Convenience adapter for provider/model UI records."""
    return enrich_provider_info(info)


__all__ = ["install_provider_links_route", "enrich"]
