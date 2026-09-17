"""Deterministic agent-slot -> Model Fabric routing requirements.

Agent slots are logical specialists. This module does not invent models; it
translates a slot into a capability-aware ModelRequest that the existing
Model Fabric can route to verified/configured providers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple

from forge.agents.fleet import AgentSlot
from forge.models.request import ModelRequest


@dataclass(frozen=True)
class AgentRouteSpec:
    """Routing requirements derived from an agent's domain and specialty."""

    capabilities: Tuple[str, ...]
    complexity: float = 1.0
    prefer_local: bool = False
    prefer_free: bool = True


_DOMAIN_CAPABILITIES: Dict[str, Tuple[str, ...]] = {
    "frontend": ("coding", "vision"),
    "backend": ("coding",),
    "database": ("coding", "reasoning"),
    "api": ("coding", "reasoning"),
    "devops": ("coding", "automation"),
    "security": ("coding", "security"),
    "testing": ("coding", "reasoning"),
    "debugging": ("coding", "reasoning"),
    "performance": ("coding", "reasoning"),
    "architecture": ("reasoning", "coding"),
    "research": ("research", "reasoning"),
    "documentation": ("writing", "reasoning"),
    "data": ("coding", "data"),
    "ml": ("coding", "reasoning"),
    "llm": ("coding", "reasoning"),
    "vision": ("vision", "reasoning"),
    "audio": ("audio", "reasoning"),
    "video": ("video", "vision"),
    "3d": ("3d", "vision", "coding"),
    "game": ("coding", "3d", "reasoning"),
    "mobile": ("coding",),
    "desktop": ("coding",),
    "cloud": ("coding", "devops"),
    "distributed": ("coding", "reasoning"),
    "embedded": ("coding",),
    "os": ("coding", "systems"),
    "kernel": ("coding", "systems"),
    "networking": ("coding", "networking"),
    "automation": ("coding", "automation"),
    "observability": ("coding", "reasoning"),
    "product": ("reasoning", "writing"),
    "ux": ("vision", "writing", "reasoning"),
    "qa": ("coding", "reasoning"),
}

_HIGH_COMPLEXITY = {
    "planner", "architect", "researcher", "security-auditor",
    "performance-auditor", "reliability-engineer", "incident-responder",
    "verifier", "benchmark-engineer", "dependency-auditor",
    "compatibility-engineer", "coordinator",
}


def route_spec(slot: AgentSlot) -> AgentRouteSpec:
    """Return deterministic requirements for one logical agent slot."""
    base = _DOMAIN_CAPABILITIES.get(slot.domain, ("reasoning",))
    capabilities = tuple(dict.fromkeys(base + (slot.specialty,)))
    complexity = 2.0 if slot.specialty in _HIGH_COMPLEXITY else 1.25
    return AgentRouteSpec(
        capabilities=capabilities,
        complexity=complexity,
        prefer_local=slot.domain in {"automation", "desktop"},
        prefer_free=True,
    )


def model_request_for_agent(
    slot: AgentSlot,
    *,
    prompt: str = "",
    task: str = "",
    caller: str = "",
    **kwargs: Any,
) -> ModelRequest:
    """Build the canonical ModelRequest for an agent slot.

    The returned request is only a routing contract. The actual model/provider
    is selected later by Model Fabric and must satisfy its verification and
    availability rules.
    """
    spec = route_spec(slot)
    return ModelRequest(
        prompt=prompt,
        task=task or slot.name,
        caller=caller or "mediated-agent:" + slot.name,
        capability=spec.capabilities[0],
        required_capabilities=spec.capabilities,
        complexity=spec.complexity,
        prefer_free=spec.prefer_free,
        prefer_local=spec.prefer_local,
        metadata={
            "agent_name": slot.name,
            "agent_domain": slot.domain,
            "agent_specialty": slot.specialty,
            "routing_schema_version": 1,
        },
        **kwargs,
    )


def route_snapshot(slot: AgentSlot) -> Dict[str, Any]:
    """Safe, content-free routing metadata for cockpit/API inspection."""
    spec = route_spec(slot)
    return {
        "schema_version": 1,
        "agent": slot.name,
        "domain": slot.domain,
        "specialty": slot.specialty,
        "required_capabilities": list(spec.capabilities),
        "complexity": spec.complexity,
        "prefer_local": spec.prefer_local,
        "prefer_free": spec.prefer_free,
        "execution": "delegated-to-model-fabric",
    }
