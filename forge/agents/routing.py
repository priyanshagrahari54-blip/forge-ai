"""Deterministic agent-slot -> Model Fabric routing requirements."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple

from forge.agents.fleet import AgentSlot
from forge.models.capabilities import ALL_CAPABILITIES
from forge.models.request import ModelRequest


@dataclass(frozen=True)
class AgentRouteSpec:
    capabilities: Tuple[str, ...]
    complexity: float = 1.0
    prefer_local: bool = False
    prefer_free: bool = True


# Only canonical Model Fabric capabilities are emitted here. Domain-specific
# labels remain agent metadata and never become invented model capabilities.
_DOMAIN_CAPABILITIES: Dict[str, Tuple[str, ...]] = {
    "frontend": ("coding", "vision"), "backend": ("coding",),
    "database": ("coding", "reasoning"), "api": ("coding", "reasoning"),
    "devops": ("coding", "tool_use"), "security": ("coding", "security"),
    "testing": ("coding", "testing"), "debugging": ("coding", "debugging"),
    "performance": ("coding", "reasoning"), "architecture": ("reasoning", "planning"),
    "research": ("research", "reasoning"), "documentation": ("documentation", "reasoning"),
    "data": ("coding", "reasoning"), "ml": ("coding", "reasoning"),
    "llm": ("coding", "reasoning"), "vision": ("vision", "reasoning"),
    "audio": ("audio", "reasoning"), "video": ("vision", "audio"),
    "3d": ("vision", "coding"), "game": ("coding", "vision", "reasoning"),
    "mobile": ("coding",), "desktop": ("coding",), "cloud": ("coding", "tool_use"),
    "distributed": ("coding", "reasoning"), "embedded": ("coding",),
    "os": ("coding", "reasoning"), "kernel": ("coding", "reasoning"),
    "networking": ("coding", "tool_use"), "automation": ("coding", "tool_use"),
    "observability": ("coding", "reasoning"), "product": ("reasoning", "documentation"),
    "ux": ("vision", "documentation", "reasoning"), "qa": ("testing", "reasoning"),
}

_SPECIALTY_CAPABILITY = {
    "planner": "planning", "builder": "coding", "reviewer": "review",
    "tester": "testing", "debugger": "debugging", "optimizer": "reasoning",
    "researcher": "research", "integrator": "tool_use", "security-auditor": "security",
    "performance-auditor": "reasoning", "release-engineer": "tool_use",
    "documentation-engineer": "documentation", "migration-engineer": "coding",
    "reliability-engineer": "reasoning", "incident-responder": "reasoning",
    "verifier": "reasoning", "critic": "review", "synthesizer": "reasoning",
    "explorer": "research", "validator": "testing", "benchmark-engineer": "testing",
    "dependency-auditor": "security", "compatibility-engineer": "coding",
    "refactorer": "coding", "designer": "vision", "implementer": "coding",
    "maintainer": "coding", "operator": "tool_use", "coordinator": "planning",
    "specialist": "reasoning",
}

_HIGH_COMPLEXITY = {
    "planner", "researcher", "security-auditor", "performance-auditor",
    "reliability-engineer", "incident-responder", "verifier", "benchmark-engineer",
    "dependency-auditor", "compatibility-engineer", "coordinator",
}


def route_spec(slot: AgentSlot) -> AgentRouteSpec:
    """Return deterministic, canonical Model Fabric requirements."""
    base = _DOMAIN_CAPABILITIES.get(slot.domain, ("reasoning",))
    specialty_cap = _SPECIALTY_CAPABILITY.get(slot.specialty, "reasoning")
    values = tuple(dict.fromkeys(base + (specialty_cap,)))
    capabilities = tuple(value for value in values if value in ALL_CAPABILITIES)
    return AgentRouteSpec(
        capabilities=capabilities or ("reasoning",),
        complexity=2.0 if slot.specialty in _HIGH_COMPLEXITY else 1.25,
        prefer_local=slot.domain in {"automation", "desktop"},
        prefer_free=True,
    )


def model_request_for_agent(slot: AgentSlot, *, prompt: str = "", task: str = "", caller: str = "", **kwargs: Any) -> ModelRequest:
    """Build the canonical ModelRequest; Model Fabric selects the real model."""
    spec = route_spec(slot)
    return ModelRequest(
        prompt=prompt, task=task or slot.name,
        caller=caller or "mediated-agent:" + slot.name,
        capability=spec.capabilities[0], required_capabilities=spec.capabilities,
        complexity=spec.complexity, prefer_free=spec.prefer_free,
        prefer_local=spec.prefer_local,
        metadata={"agent_name": slot.name, "agent_domain": slot.domain,
                  "agent_specialty": slot.specialty, "routing_schema_version": 1},
        **kwargs,
    )


def route_snapshot(slot: AgentSlot) -> Dict[str, Any]:
    """Safe, content-free routing metadata for cockpit/API inspection."""
    spec = route_spec(slot)
    return {"schema_version": 1, "agent": slot.name, "domain": slot.domain,
            "specialty": slot.specialty, "required_capabilities": list(spec.capabilities),
            "complexity": spec.complexity, "prefer_local": spec.prefer_local,
            "prefer_free": spec.prefer_free, "execution": "delegated-to-model-fabric"}
