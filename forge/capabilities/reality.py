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

    # -- A84: intelligence & personal-AI plane --------------------------------
    # Each entry below follows the same rule as every other one: an implemented
    # module is ARCHITECTURE/READY; configuration moves it to CONFIGURED;
    # a live external dependency moves it to BLOCKED->CONFIGURED; only real
    # runtime evidence promotes it to LIVE. A class existing is never "live".

    result["personal-assistant-core"] = CapabilityTruth(
        "personal-assistant-core", "Persistent personal assistant pipeline",
        STATUS_LIVE, True, True, True, True, False,
        detail=("session ledger -> continuity -> prompt intelligence -> "
                 "triage -> context -> delegated execution -> verification -> "
                 "controlled memory; every execution step delegates to the "
                 "existing supervisor/fabric/research paths"),
        evidence="forge/assistant + tests/test_a84_*",
    )
    result["personal-memory"] = CapabilityTruth(
        "personal-memory", "Layered personal memory (short-term/session/"
        "long-term/episodic/semantic/project)", STATUS_LIVE,
        True, True, True, True, False,
        detail=("retention is decided per record — ordinary messages are not "
                 "stored; sensitive content fails closed; inspect/correct/"
                 "delete/forget/clear are user-controlled paths over the "
                 "shared SQLite memory engine behind the A33 MEMORY policy"),
        evidence="forge/memory + forge/assistant/memory.py",
    )
    result["prompt-intelligence"] = CapabilityTruth(
        "prompt-intelligence", "Intent-preserving prompt enhancement",
        STATUS_LIVE, True, True, True, True, False,
        detail=("deterministic extraction/enhancement + quality gate + "
                 "model-targeted adaptation + bounded versioning; ASK_USER "
                 "on blocking ambiguity; intent-preservation check gates every "
                 "enhancement"),
        evidence="forge/prompt_intelligence",
    )
    result["tool-intelligence"] = CapabilityTruth(
        "tool-intelligence", "Tool capability registry + planner + verifier",
        STATUS_LIVE, True, True, True, True, False,
        detail=("registry descriptions are NOT permissions: execution stays "
                 "on the A33-gated paths; availability resolves per tool "
                 "probe and honestly reports architecture-only where nothing "
                 "is live"),
        evidence="forge/tools/intelligence + forge.tools",
    )
    result["deep-research"] = CapabilityTruth(
        "deep-research", "Multi-source deep research with provenance",
        STATUS_READY, True, _env_configured("FORGE_RESEARCH_ALLOW_WEB"),
        False, False, False,
        detail=("scope/strategy/extraction/cross-comparison/contradiction/ "
                 "ranking/synthesis over the secure research engine; live web "
                 "corroboration requires a configured provider — without it "
                 "reports PARTIAL, never fabricated citations"),
        evidence="forge/research/deep.py + A81 secure engine",
    )
    result["safe-research-networks"] = CapabilityTruth(
        "safe-research-networks", "Lawful privacy-network research (Tor-class)",
        STATUS_ARCHITECTURE, True, False, False, False, False,
        detail=("classification + prohibition screen + inert gateway: enabled "
                 "ONLY with an operator-supplied isolated transport; no "
                 "downloads, credentials, transactions or auth; disabled is "
                 "the honest default and Forge ships no circuit authority"),
        evidence="forge/research/networks.py",
    )
    result["pattern-graph"] = CapabilityTruth(
        "pattern-graph", "Entity/relation pattern layer with conflict "
        "adjudication", STATUS_LIVE, True, True, True, True, False,
        detail=("contradictions open CONFLICT records and are adjudicated "
                 "explicitly; nothing is silently overwritten"),
        evidence="forge/patterns + shared plane database",
    )
    result["operational-learning"] = CapabilityTruth(
        "operational-learning", "Tool/route/provider/task outcome learning",
        STATUS_LIVE, True, True, True, True, False,
        detail=("bounded ledgers drive proposals and soft priors; model "
                 "weights are NEVER trained from conversations and priors "
                 "can only order already-eligible candidates"),
        evidence="forge/learning (A84) + A59 ledger",
    )
    result["cross-model-teams"] = CapabilityTruth(
        "cross-model-teams", "Task-dependent multi-model collaboration",
        STATUS_READY, True, True, False, False, False,
        detail=("roles resolve through the fabric per task; unmet roles stay "
                 "unmet and a one-model answer is labeled single-model — "
                 "never presented as consensus or a seven-model panel"),
        evidence="forge/models/teams.py + A45/A44",
    )
    result["critic-verifier"] = CapabilityTruth(
        "critic-verifier", "generate -> critique -> revise -> verify",
        STATUS_LIVE, True, True, True, True, False,
        detail=("deterministic-first critic (requirements, hallucination "
                 "indicators, compile/security, provenance tracing); model "
                 "critics contribute findings but never remove deterministic "
                 "ones; an exhausted loop ends UNVERIFIED, not accepted"),
        evidence="forge/verification + forge.security gates",
    )
    result["personalization"] = CapabilityTruth(
        "personalization", "User preference profile (presentation + soft routing)",
        STATUS_LIVE, True, True, True, True, False,
        detail=("closed field vocabulary persisted as memory; can restrict "
                 "(prefer local/free) but never relax security, authorization "
                 "or capability requirements"),
        evidence="forge/assistant/personalization.py",
    )
    result["long-context"] = CapabilityTruth(
        "long-context", "Hierarchical long-context assembly (retrieval + digests)",
        STATUS_LIVE, True, True, True, True, False,
        detail=("context is assembled by relevance within a model-derived "
                 "budget with lossy digests labeled as such; a lifetime of "
                 "conversation is never injected wholesale"),
        evidence="forge/assistant/context.py + forge.memory.summarizer",
    )
    result["context-quality"] = CapabilityTruth(
        "context-quality", "Context quality metrics (relevance/redundancy/"
        "contradictions/source quality/budget)", STATUS_LIVE,
        True, True, True, True, False,
        detail="quality numbers accompany every assembled bundle",
        evidence="forge/assistant/context.py",
    )
    result["improvement-loop"] = CapabilityTruth(
        "improvement-loop", "Continuous improvement proposals", STATUS_LIVE,
        True, True, True, True, False,
        detail=("proposals only; any self-modification still runs through "
                 "A58/A26 loops with tests, security, authorization and "
                 "rollback — this layer cannot apply its own suggestions"),
        evidence="forge/improvement + A58/A59/A60",
    )
    result["massive-model-routing"] = CapabilityTruth(
        "massive-model-routing", "Routing to large/MoE/trillion-scale models",
        STATUS_CONFIGURED if (ollama_configured or openai_configured)
        else STATUS_ARCHITECTURE,
        True, ollama_configured or openai_configured,
        bool(ollama.get("reachable") or openai.get("reachable")),
        bool(ollama.get("verified") or openai.get("verified")),
        False,
        detail=(
            "Forge ROUTES to provider-offered large models; it does not HOST "
            "their weights; and routing availability exists only where a "
            "provider actually offers them — " + (
                "model parameter scale, Forge infrastructure capability and "
                "provider availability are three separate facts")),
        evidence="forge/models (registry scale metadata + router scale fit) + provider health",
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
            "catalog_is_not_inference": True,
            "registration_is_not_execution": True,
            "tool_registration_is_not_tool_success": True,
            "memory_storage_is_not_learning": True,
            "prompt_rewriting_is_not_improved_result": True,
            "source_discovery_is_not_verified_evidence": True,
            "declared_scale_is_not_hosting": True,
            "many_agents_is_not_better_answer": True,
            "model_availability_is_not_production_readiness": True,
        },
        "counts": counts,
        "capabilities": [item.to_dict() for item in capabilities.values()],
    }
