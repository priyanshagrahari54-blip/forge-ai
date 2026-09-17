"""Single truthful runtime snapshot for the complete Forge stack.

This module is intentionally an observer, not a second scheduler. It composes
existing capability truth, worker admission state, model-fabric health,
execution contracts, and the G560 thin-client profile into one machine-readable
snapshot. It never upgrades ARCHITECTURE/CONFIGURED/SIMULATED to LIVE.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from forge.capabilities.reality import capability_snapshot
from forge.diagnostics.readiness import build_readiness
from forge.client.thin_client import select_profile


def _provider_health(fabric: Any) -> Optional[Mapping[str, Any]]:
    if fabric is None:
        return None
    for name in ("health", "health_snapshot", "snapshot"):
        fn = getattr(fabric, name, None)
        if not callable(fn):
            continue
        try:
            value = fn()
        except Exception:
            continue
        if isinstance(value, Mapping):
            return value.get("providers") or value
    return None


def runtime_snapshot(*, fabric: Any = None, worker_registry: Any = None,
                     ram_mb: int = 2048, cpu_threads: int = 2) -> dict[str, Any]:
    """Return one truthful snapshot of the integrated Forge runtime.

    ``ram_mb`` and ``cpu_threads`` are client-side hints used only to select
    the UI/resource profile. They do not alter server capability truth.
    """
    capabilities = capability_snapshot(provider_health=_provider_health(fabric))
    readiness = build_readiness(
        provider_health=_provider_health(fabric),
        worker_registry=worker_registry,
    )
    client = select_profile(ram_mb=ram_mb, cpu_threads=cpu_threads)
    return {
        "schema_version": 1,
        "capabilities": capabilities,
        "readiness": readiness,
        "client_profile": client.to_dict(),
        "execution": {
            "local_default": True,
            "remote_requires_admission": True,
            "remote_transport_must_be_authenticated": True,
            "sandbox_policy_requires_backend_enforcement": True,
            "external_provider_state_is_runtime_verified": True,
        },
    }
