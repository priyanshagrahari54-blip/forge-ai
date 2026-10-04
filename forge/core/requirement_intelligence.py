"""Requirement Intelligence: Forge's pre-execution contract gate.

This module is deliberately provider-neutral and deterministic. It does not
pretend that keyword extraction is semantic understanding; a model-backed
adapter may enrich the contract later. The important invariant is that every
execution starts from an explicit contract and unresolved ambiguity is visible.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import re
from typing import Iterable


class ContractStatus(str, Enum):
    READY = "ready"
    NEEDS_CLARIFICATION = "needs_clarification"


@dataclass
class RequirementContract:
    user_intent: str
    explicit_requirements: list[str] = field(default_factory=list)
    implicit_requirements: list[str] = field(default_factory=list)
    quality_requirements: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    failure_conditions: list[str] = field(default_factory=list)
    verification_methods: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    ambiguities: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    status: str = ContractStatus.READY.value

    @property
    def executable(self) -> bool:
        return self.status == ContractStatus.READY.value and not self.ambiguities and not self.contradictions

    def to_dict(self) -> dict:
        return asdict(self)


_QUALITY_WORDS = (
    "professional", "production", "production-grade", "enterprise",
    "premium", "aaa", "high quality", "gta-like", "cinematic",
)
_CAPABILITY_RULES = {
    "software-engineering": r"\b(code|coding|software|app|application|website|api|saas|program)\b",
    "research": r"\b(research|paper|arxiv|study|investigate|sources?)\b",
    "browser-computer-use": r"\b(browser|website|computer|desktop|click|open|navigate)\b",
    "creative-media": r"\b(video|image|audio|vfx|motion|editing|after effects|photoshop)\b",
    "3d-blender": r"\b(3d|blender|modeling|render)\b",
    "game-development": r"\b(game|multiplayer|open.world|aaa)\b",
    "os-systems": r"\b(operating system|kernel|driver|os|linux|windows|android)\b",
    "ai-modeling": r"\b(model|llm|rag|fine.?tun|neural network|agent)\b",
}


def _clean(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(x.strip() for x in items if x and x.strip()))


def analyze(request: str, *, constraints: Iterable[str] = (),
            resources: Iterable[str] = (), references: Iterable[str] = ()) -> RequirementContract:
    """Create a conservative first-pass contract.

    Missing information is never silently converted into a false promise.
    Complex/high-quality requests receive explicit verification requirements.
    """
    statement = request.strip()
    if not statement:
        raise ValueError("Cannot analyze an empty requirement")

    quality = [f"Meet the requested {word} quality bar."
               for word in _QUALITY_WORDS if word in statement.lower()]
    capabilities = [
        name for name, pattern in _CAPABILITY_RULES.items()
        if re.search(pattern, statement, flags=re.I)
    ]
    explicit = [statement]
    acceptance = [
        "All explicit requirements are mapped to an implementation or an explicit limitation.",
        "Required tests and verification evidence pass.",
        "The final result is checked against the contract before delivery.",
    ]
    failures = [
        "Do not report completion without evidence.",
        "Block delivery when a required acceptance criterion fails.",
        "Do not silently downgrade the requested quality bar.",
    ]
    verification = ["requirement-to-result audit", "automated tests", "security/permission checks"]

    # A quality-sensitive request without a concrete reference or measurable
    # target is not rejected outright, but the missing decision is surfaced.
    ambiguities: list[str] = []
    if quality and not any(token in statement.lower()
                           for token in ("reference", "benchmark", "criteria", "target", "spec")):
        ambiguities.append(
            "Quality bar is named but no measurable/reference target is supplied; "
            "a quality target must be confirmed before claiming final acceptance."
        )

    return RequirementContract(
        user_intent=statement,
        explicit_requirements=explicit,
        quality_requirements=_clean(quality),
        references=_clean(references),
        constraints=_clean(constraints),
        resources=_clean(resources),
        acceptance_criteria=_clean(acceptance),
        failure_conditions=_clean(failures),
        verification_methods=_clean(verification),
        capabilities=_clean(capabilities),
        ambiguities=_clean(ambiguities),
        status=(ContractStatus.NEEDS_CLARIFICATION.value if ambiguities
                else ContractStatus.READY.value),
    )
