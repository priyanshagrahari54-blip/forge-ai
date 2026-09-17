from __future__ import annotations

from dataclasses import dataclass
from itertools import product


# These are capability labels, not claims that every label has a unique model.
# The fleet is intentionally generated from composable specialties so Forge can
# address 1000+ distinct work roles without pretending there are 1000 separate
# frontier intelligences installed locally.
DOMAINS = (
    "frontend", "backend", "database", "api", "devops", "security", "testing",
    "debugging", "performance", "architecture", "research", "documentation",
    "data", "ml", "llm", "vision", "audio", "video", "3d", "game",
    "mobile", "desktop", "cloud", "distributed", "embedded", "os", "kernel",
    "networking", "automation", "observability", "product", "ux", "qa",
)
SPECIALTIES = (
    "planner", "builder", "reviewer", "tester", "debugger", "optimizer",
    "researcher", "integrator", "security-auditor", "performance-auditor",
    "release-engineer", "documentation-engineer", "migration-engineer",
    "reliability-engineer", "incident-responder", "verifier", "critic",
    "synthesizer", "explorer", "validator", "benchmark-engineer",
    "dependency-auditor", "compatibility-engineer", "refactorer", "designer",
    "implementer", "maintainer", "operator", "coordinator", "specialist",
)


@dataclass(frozen=True)
class AgentSlot:
    name: str
    role: str
    domain: str
    specialty: str
    capabilities: tuple[str, ...]
    priority: int = 50


def build_fleet(minimum: int = 1000) -> tuple[AgentSlot, ...]:
    """Create a deterministic 1000+ logical specialist fleet.

    A slot is a routable role. Actual execution is delegated to configured
    Model Fabric providers, so a slot never implies a nonexistent model.
    """
    if minimum < 1:
        raise ValueError("minimum must be positive")
    slots: list[AgentSlot] = []
    for domain, specialty in product(DOMAINS, SPECIALTIES):
        name = f"{domain}-{specialty}"
        capabilities = (domain, specialty, "engineering")
        slots.append(AgentSlot(name, specialty, domain, specialty, capabilities))
    if len(slots) < minimum:
        # Expand deterministically with numbered specialist slots only when a
        # caller asks for more than the base Cartesian fleet.
        index = 1
        while len(slots) < minimum:
            domain = DOMAINS[(index - 1) % len(DOMAINS)]
            specialty = SPECIALTIES[(index - 1) % len(SPECIALTIES)]
            slots.append(
                AgentSlot(
                    f"{domain}-{specialty}-{index:04d}",
                    specialty,
                    domain,
                    specialty,
                    (domain, specialty, "engineering"),
                    priority=40,
                )
            )
            index += 1
    return tuple(slots[:minimum])


DEFAULT_AGENT_FLEET = build_fleet(1000)


def fleet_snapshot() -> dict[str, object]:
    return {
        "schema_version": 1,
        "count": len(DEFAULT_AGENT_FLEET),
        "domains": sorted({slot.domain for slot in DEFAULT_AGENT_FLEET}),
        "specialties": sorted({slot.specialty for slot in DEFAULT_AGENT_FLEET}),
        "mode": "logical-routable-fleet",
        "execution": "delegated-to-configured-model-fabric",
    }
