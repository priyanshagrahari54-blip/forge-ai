"""Evidence-based Forge runtime readiness snapshot.

The report is intentionally useful for the cockpit and operators without
turning architecture into a claim of live external infrastructure.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from forge.capabilities.reality import capability_snapshot


def build_readiness(*, provider_health: Optional[Mapping[str, Mapping[str, Any]]] = None,
                    worker_registry: Any = None) -> dict[str, Any]:
    capabilities = capability_snapshot(provider_health=provider_health)
    workers = worker_registry.snapshot() if worker_registry is not None else None
    return {
        "schema_version": 1,
        "system": "forge-ai",
        "capabilities": capabilities,
        "workers": workers,
        "execution_contract": {
            "local_execution": "real-bounded-subprocess",
            "remote_execution": "requires-explicit-worker-admission-and-configured-transport",
            "sandbox_policy": "declarative-unless-enforced-by-backend",
            "background_execution": "server-owned-persistent-task-lifecycle",
            "lease_recovery": "existing-heartbeat-and-stale-lease-recovery",
        },
        "truth_rules": [
            "Configured credentials are not proof of reachability.",
            "A worker registration is not proof of executable transport.",
            "Architecture and simulation are never reported as live execution.",
        ],
    }
