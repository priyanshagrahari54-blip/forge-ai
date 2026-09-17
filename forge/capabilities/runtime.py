"""Runtime evidence adapter for the capability reality registry."""
from __future__ import annotations

from typing import Any, Dict, Mapping

from forge.capabilities.reality import capability_snapshot


def provider_health_from_fabric(fabric: Any) -> Dict[str, Mapping[str, Any]]:
    """Extract conservative provider evidence from an existing Model Fabric.

    The adapter accepts several provider implementations without requiring a
    common concrete class. A provider is considered *reachable* only when its
    own health method explicitly says so. No API-key presence is promoted to
    verified state.
    """
    result: Dict[str, Mapping[str, Any]] = {}
    providers = getattr(fabric, "providers", None)
    if callable(providers):
        try:
            providers = providers()
        except Exception:
            providers = None
    if isinstance(providers, Mapping):
        items = providers.items()
    elif providers:
        items = ((str(getattr(item, "name", "")), item) for item in providers)
    else:
        items = ()

    for name, provider in items:
        provider_id = str(name or getattr(provider, "name", "") or "").lower()
        if not provider_id:
            continue
        health_fn = getattr(provider, "health", None)
        payload: Any = {}
        if callable(health_fn):
            try:
                payload = health_fn() or {}
            except Exception as exc:
                payload = {"available": False, "error": str(exc)[:200]}
        if not isinstance(payload, Mapping):
            payload = {}
        result[provider_id] = {
            "configured": bool(payload.get("configured", True)),
            "reachable": bool(payload.get("reachable", payload.get("available", False))),
            "verified": bool(payload.get("verified", False)),
        }
    return result


def runtime_capability_snapshot(fabric: Any = None) -> Dict[str, Any]:
    """Build a capability snapshot using live fabric evidence when available."""
    health = provider_health_from_fabric(fabric) if fabric is not None else {}
    return capability_snapshot(provider_health=health)
