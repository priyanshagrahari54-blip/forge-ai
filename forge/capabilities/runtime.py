"""Runtime evidence adapter for the capability reality registry."""
from __future__ import annotations

from typing import Any, Dict, Mapping

from forge.capabilities.reality import capability_snapshot


def _provider_items(providers: Any) -> list:
    """Return ``[(name, provider), ...]`` for every supported registry shape.

    A real :class:`~forge.models.provider.ProviderRegistry` exposes
    ``items()`` but is not itself iterable; plain mappings and iterables are
    also accepted so adapters and test doubles keep working.
    """
    if providers is None:
        return []
    if isinstance(providers, Mapping):
        return list(providers.items())
    getter = getattr(providers, "items", None)
    if callable(getter):
        try:
            return list(getter())
        except Exception:
            return []
    names = getattr(providers, "names", None)
    if callable(names):
        lookup = getattr(providers, "get", None)
        result = []
        try:
            for name in names() or ():
                try:
                    result.append((name, lookup(name) if callable(lookup) else None))
                except Exception:
                    continue
        except Exception:
            return []
        return result
    try:
        return [(str(getattr(item, "name", "")), item) for item in providers]
    except TypeError:
        return []


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
    items = _provider_items(providers)

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
    """Build the *wire* capability snapshot, using live fabric evidence.

    The wire contract publishes ``schema_version`` as a string (the domain
    snapshot keeps the integer), so API consumers can compare versions
    without depending on JSON number formatting.
    """
    health = provider_health_from_fabric(fabric) if fabric is not None else {}
    payload = capability_snapshot(provider_health=health)
    payload["schema_version"] = str(payload.get("schema_version", "1"))
    # Wire aliases: the HTTP contract addresses each capability by ``id`` and
    # reads its evidence state from ``state``. The domain field names are kept
    # so both consumers work off one payload.
    payload["capabilities"] = [
        _wire_capability(item) for item in payload.get("capabilities", [])
    ]
    return payload


def _wire_capability(item: Mapping[str, Any]) -> Dict[str, Any]:
    """Add the ``id``/``state`` wire aliases to one capability record."""
    record = dict(item)
    if "id" not in record and "capability_id" in record:
        record["id"] = record["capability_id"]
    if "state" not in record and "status" in record:
        record["state"] = record["status"]
    return record
