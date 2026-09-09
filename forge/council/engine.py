"""AI Council (A45): independent model opinions → lead review →
consensus with disagreements flagged and minorities preserved.

Honesty rules:

* The A45 council runs over *simulated* model members — deterministic,
  distinct stances, every opinion labeled ``simulation=True`` with its
  member/model name. Real model providers plug in behind the same
  ``CouncilMember`` protocol later; this build never pretends real
  models were consulted.
* Agreement is computed from actual opinion stances — the lead never
  invents unanimity. Disagreements are always flagged, minority
  opinions are preserved verbatim, and final confidence is the honest
  fraction of agreeing members.
* The council answers questions only. It executes nothing, writes
  nothing, and its verdict can never authorize an action — it is
  advisory input, exactly like model output.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

MAX_MEMBERS = 5
MAX_QUESTION = 4000
MAX_OPINION = 3000


class CouncilMember(Protocol):
    name: str
    model: str
    stance: str

    def deliberate(self, question: str) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class MemberOpinion:
    member: str
    model: str
    stance: str
    stance_label: str
    reasoning: str
    confidence: float
    simulation: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "member": self.member,
            "model": self.model,
            "stance": self.stance,
            "stance_label": self.stance_label,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "simulation": self.simulation,
        }


class SimulatedCouncilModel:
    """A deterministic simulated council member with a distinct stance.

    Stances are fixed per member so deliberation is reproducible and
    testable; every opinion states the simulation explicitly.
    """

    def __init__(self, name: str, *, stance: str,
                 stance_label: str) -> None:
        self.name = name
        self.model = f"council-sim-{name}"
        self.stance = stance
        self.stance_label = stance_label

    def deliberate(self, question: str) -> dict[str, Any]:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be non-empty")
        snippet = question.strip()[:MAX_QUESTION][:90]
        return MemberOpinion(
            member=self.name, model=self.model, stance=self.stance,
            stance_label=self.stance_label,
            reasoning=(
                f"[simulated council member {self.name} "
                f"({self.stance_label})] On: {snippet} — "
                "this is a deterministic simulated opinion with no "
                "real model behind it."),
            confidence=0.5, simulation=True).to_dict()


DEFAULT_MEMBERS = (
    SimulatedCouncilModel("alpha", stance="approve",
                          stance_label="risk-averse: proceed only with "
                                       "narrow scope and operator gates"),
    SimulatedCouncilModel("beta", stance="approve",
                          stance_label="balanced: proceed with tests "
                                       "and rollback plan"),
    SimulatedCouncilModel("gamma", stance="reject",
                          stance_label="skeptic: requires more evidence "
                                       "before proceeding"),
)


class AICouncilEngine:
    """Lead-reviewed multi-model deliberation, bounded and honest."""

    def __init__(self, members: list[CouncilMember] | None = None) -> None:
        if members is not None and not members:
            self.members = []
        else:
            self.members = list(members if members is not None
                                else DEFAULT_MEMBERS)[:MAX_MEMBERS]

    def deliberate(self, question: str) -> dict[str, Any]:
        if not isinstance(question, str) or not question.strip() \
                or len(question) > MAX_QUESTION:
            raise ValueError("question must be 1-4000 characters")
        if not self.members:
            raise ValueError("a council needs at least one member")
        started = time.time()
        opinions = []
        for member in self.members:
            opinions.append(member.deliberate(question))
        stances = [opinion["stance"] for opinion in opinions]
        counts: dict[str, int] = {}
        for stance in stances:
            counts[stance] = counts.get(stance, 0) + 1
        majority = max(counts, key=lambda stance: counts[stance])
        agreeing = [opinion for opinion in opinions
                    if opinion["stance"] == majority]
        disagreeing = [opinion for opinion in opinions
                       if opinion["stance"] != majority]
        consensus = not disagreeing
        confidence = round(len(agreeing) / len(opinions), 3)
        lead = agreeing[0]
        final_answer = (
            f"Council verdict ({len(agreeing)}/{len(opinions)} agree): "
            f"{majority}. {lead['reasoning'][:800]}")
        if disagreeing:
            flagged = "; ".join(
                f"{opinion['member']} ({opinion['stance_label']})"
                for opinion in disagreeing)[:400]
            final_answer += f" DISAGREEMENT FLAGGED: {flagged}."
        return {
            "question": question[:400],
            "opinions": opinions,
            "agreements": [opinion["member"] for opinion in agreeing],
            "disagreements": [opinion["member"] for opinion in disagreeing],
            "minority_opinions": [opinion for opinion in disagreeing],
            "final_answer": final_answer[:2000],
            "stance": majority,
            "consensus": consensus,
            "confidence": confidence,
            "simulation": True,
            "members": len(self.members),
            "elapsed_ms": round((time.time() - started) * 1000, 1),
        }
