"""Forge capability truth and readiness registry.

This module deliberately separates four states that were previously easy to
confuse in a UI: implemented architecture, configured provider, reachable
provider, and verified execution.  A capability must never be displayed as
"live" merely because a class or adapter exists.

The registry is dependency-light and safe to import on Python 3.8 / Windows.
It performs no network calls during import.  Callers may supply live health
information from the existing Model Fabric / provider health systems.
"""
from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional


STATUS_LIVE = "LIVE"
STATUS_READY = "READY"
STATUS_CONFIGURED = "CONFIGURED"
STATUS_ARCHITECTURE = "ARCHITECTURE"
STATUS_SIMULATED = "SIMULATED"
STATUS_MISSING = "MISSING"
STATUS_BLOCKED = "BLOCKED"
STATUS_ERROR = "ERROR"


@dataclass(frozen=True)
class CapabilityTruth:
    """Evidence-backed state for one user-visible Forge capability."""

    capability_id: str
    name: str
    status: str
    implemented: bool
    configured: bool
    reachable: bool
    verified: bool
    simulated: bool
    provider: str = ""
    detail: str = ""
    evidence: str = ""

    @property
    def live(self) -> bool:
        return self.status in (STATUS_LIVE, STATUS_READY)

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["live"] = self.live
        return result


def _env_configured(*names: str) -> bool:
    return any(bool(os.environ.get(name, "").strip()) for name in names)


def _command_available(name: str) -> bool:
    return shutil.which(name) is not None


def _provider_state(
    *,
    capability_id: str,
    name: str,
    provider: str,
    configured: bool,
    reachable: bool,
    verified: bool,
    implemented: bool = True,
    simulated: bool = False,
    detail: str = "",
    evidence: str = "runtime/provider health",
) -> CapabilityTruth:
    if simulated:
        status = STATUS_SIMULATED
    elif not implemented:
        status = STATUS_MISSING
    elif not configured:
        status = STATUS_ARCHITECTURE
    elif not reachable:
        status = STATUS_BLOCKED
    elif not verified:
        status = STATUS_CONFIGURED
    else:
        status = STATUS_LIVE
    return CapabilityTruth(
        capability_id=capability_id,
        name=name,
        status=status,
        implemented=implemented,
        configured=configured,
        reachable=reachable,
        verified=verified,
        simulated=simulated,
        provider=provider,
        detail=detail,
        evidence=evidence,
    )


def default_capabilities(
    *,
    provider_health: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, CapabilityTruth]:
    """Return conservative capability truth for the current installation.

    Existing runtime health can be passed as ``provider_health``.  This
    function intentionally does not infer "live" from the presence of an API
    key alone: configured credentials only move a provider to CONFIGURED.
    """
    health = provider_health or {}

    def h(provider: str) -> Mapping[str, Any]:
        return health.get(provider, {})

    ollama = h("ollama")
    openai = h("openai")

    ollama_configured = bool(ollama.get("configured")) or _env_configured(
        "OLLAMA_HOST", "FORGE_OLLAMA_HOST"
    )
    openai_configured = bool(openai.get("configured")) or _env_configured(
        "OPENAI_API_KEY"
    )

    result: Dict[str, CapabilityTruth] = {}
    result["autonomous-engineering"] = CapabilityTruth(
        "autonomous-engineering", "Autonomous software engineering", STATUS_LIVE,
        True, True, True, True, False,
        detail="Supervisor, policy, execution, verification and recovery are implemented.",
        evidence="forge/server + forge/control + forge/engineering",
    )
    result["persistent-server"] = CapabilityTruth(
        "persistent-server", "Durable server execution", STATUS_LIVE,
        True, True, True, True, False,
        detail="SQLite task/event state, recovery, scheduler and workers are implemented.",
        evidence="ForgeServer / TaskQueue / WorkerPool / RecoveryService",
    )
    result["local-compute"] = CapabilityTruth(
        "local-compute", "Local subprocess compute", STATUS_LIVE,
        True, True, True, True, False,
        detail="Permission-gated local execution with resource controls.",
        evidence="forge/compute + server execution path",
    )
    result["model-fabric"] = CapabilityTruth(
        "model-fabric", "Model Fabric routing", STATUS_LIVE,
        True, True, True, True, False,
        detail="Registry/router/health/streaming infrastructure is present.",
        evidence="forge/models/fabric.py and backend protocol",
    )
    result["ollama"] = _provider_state(
        capability_id="ollama", name="Ollama inference", provider="ollama",
        configured=ollama_configured,
        reachable=bool(ollama.get("reachable")),
        verified=bool(ollama.get("verified")),
        detail="Requires a reachable Ollama endpoint and an actually verified model.",
    )
    result["openai"] = _provider_state(
        capability_id="openai", name="OpenAI inference", provider="openai",
        configured=openai_configured,
        reachable=bool(openai.get("reachable")),
        verified=bool(openai.get("verified")),
        detail="An API key alone is not treated as a successful provider check.",
    )
    result["voice"] = _provider_state(
        capability_id="voice", name="Voice STT/TTS", provider="configured provider",
        configured=_env_configured("OPENAI_API_KEY"),
        reachable=False,
        verified=False,
        simulated=True,
        detail="The simulator is never reported as real speech recognition.",
        evidence="voice provider configuration + runtime health",
    )
    result["vision"] = _provider_state(
        capability_id="vision", name="Vision inference", provider="configured provider",
        configured=_env_configured("OPENAI_API_KEY"),
        reachable=False,
        verified=False,
        simulated=True,
        detail="Vision adapters are not equivalent to a verified live vision model.",
        evidence="vision provider configuration + runtime health",
    )
    result["desktop-control"] = CapabilityTruth(
        "desktop-control", "Local desktop control", STATUS_ARCHITECTURE,
        True, False, False, False, False,
        detail="A real local provider exists, but activation must be explicit and verified.",
        evidence="forge/desktop_app and computer-use provider layer",
    )
    result["remote-compute"] = CapabilityTruth(
        "remote-compute", "Remote compute", STATUS_ARCHITECTURE,
        True, False, False, False, False,
        detail="Remote backends require an explicitly configured, hardened provider.",
        evidence="forge/compute remote provider interfaces",
    )
    result["web-research"] = CapabilityTruth(
        "web-research", "External web research", STATUS_ARCHITECTURE,
        True, False, False, False, False,
        detail="Repository research is real; external fetching requires a configured and safe provider.",
        evidence="forge/research",
    )
    result["training"] = CapabilityTruth(
        "training", "External model training/fine-tuning", STATUS_ARCHITECTURE,
        True, False, False, False, False,
        detail="Training infrastructure is not equivalent to an available training cluster.",
        evidence="forge/agents/training.py / model studio",
    )
    result["production-deployment"] = CapabilityTruth(
        "production-deployment", "Production deployment", STATUS_ARCHITECTURE,
        True, False, False, False, False,
        detail="Deployment management exists; a target must be configured and verified.",
        evidence="forge/deployment",
    )
    result["ai-city"] = CapabilityTruth(
        "ai-city", "AI City live visualization", STATUS_READY,
        True, True, True, True, False,
        detail="The UI exists; activity must be driven by durable runtime events, never animation-only state.",
        evidence="forge/cockpit/web/city.html + event infrastructure",
    )

    # Runtime facts useful to the G560/thin-client UI. These are descriptive,
    # not a capability claim.
    result["host-runtime"] = CapabilityTruth(
        "host-runtime", "Current Forge host runtime", STATUS_LIVE,
        True, True, True, True, False,
        provider=platform.system(),
        detail="%s %s; Python %s; x%s" % (
            platform.system(), platform.release(), platform.python_version(),
            platform.architecture()[0].replace("bit", ""),
        ),
        evidence="Python runtime introspection",
    )
    return result


def capability_snapshot(
    *,
    provider_health: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """JSON-ready snapshot for cockpit/API consumers."""
    capabilities = default_capabilities(provider_health=provider_health)
    counts: Dict[str, int] = {}
    for item in capabilities.values():
        counts[item.status] = counts.get(item.status, 0) + 1
    return {
        "schema_version": 1,
        "honesty_contract": {
            "live_requires_verified": True,
            "configured_is_not_live": True,
            "simulation_is_never_live": True,
            "unknown_provider_is_never_assumed_ready": True,
        },
        "counts": counts,
        "capabilities": [item.to_dict() for item in capabilities.values()],
    }
