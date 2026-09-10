"""AI Council (A45): independent model opinions → lead review →
consensus with disagreements flagged and minorities preserved.

Honesty rules:

* The default A45 council runs over *simulated* model members —
  deterministic, distinct stances, every opinion labeled
  ``simulation=True`` with its member/model name. Real models deliberate
  through :class:`FabricCouncilMember` (same ``CouncilMember`` protocol,
  ``simulation=False``); callers choose explicitly, and the engine
  reports ``simulation=True`` only when *every* opinion is simulated.
  This build never pretends real models were consulted.
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
from dataclasses import dataclass
from typing import Any, Protocol

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


class FabricCouncilMember:
    """A council member backed by a real model via the Model Fabric.

    Each deliberation asks the fabric (capability ``reasoning``) for a
    structured opinion under this member's role. Opinions carry
    ``simulation=False`` with the answering model name. When no model can
    serve the request — or its answer is unparseable — the member
    abstains honestly (stance ``abstain``, confidence ``0.0``, cause in
    the reasoning) instead of inventing a stance.
    """

    def __init__(self, name: str, fabric, *, role: str = "",
                 stance_hint: str = "") -> None:
        if not name or not name.strip():
            raise ValueError("member name must be non-empty")
        if fabric is None:
            raise ValueError("a fabric-backed member needs a fabric")
        self.name = name.strip()
        self.fabric = fabric
        self.role = role.strip() or f"council member {self.name}"
        self.model = "fabric/pending"
        self.stance = stance_hint.strip() or "undecided"
        self.stance_label = self.role

    def deliberate(self, question: str) -> dict[str, Any]:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be non-empty")
        from forge.models.request import ModelRequest

        prompt = (
            f"You are {self.role} on an AI advisory council. Read the "
            "question and return ONLY JSON: "
            "{stance:approve|reject|abstain, stance_label:str, "
            "reasoning:str, confidence:0..1}. Be decisive when the "
            "question warrants it; abstain only when it is unanswerable.\n"
            f"QUESTION: {question.strip()[:MAX_QUESTION]}"
        )
        try:
            response = self.fabric.generate(ModelRequest(
                prompt=prompt, capability="reasoning",
                required_capabilities=("reasoning",),
                task=question.strip()[:MAX_QUESTION],
                prefer_local=True, prefer_free=True,
                max_output_tokens=800))
        except Exception as exc:
            return self._abstain(f"model call failed: {exc}")
        if not response.success:
            return self._abstain(
                f"no reasoning model available: "
                f"{response.error or 'routing failed'}")
        try:
            data = _parse_json_object(response.text)
            stance = str(data.get("stance", "")).strip().lower()
            if stance not in ("approve", "reject", "abstain"):
                raise ValueError(f"unknown stance {stance!r}")
            confidence = float(data.get("confidence", 0.0))
            confidence = max(0.0, min(1.0, confidence))
            reasoning = str(data.get("reasoning", "")).strip()
            if not reasoning:
                raise ValueError("empty reasoning")
            label = str(data.get("stance_label", "")).strip() or self.role
        except (ValueError, AttributeError, TypeError) as exc:
            return self._abstain(f"model answer unparseable: {exc}")
        self.model = str(response.model or "fabric/unknown")
        self.stance = stance
        self.stance_label = label
        return MemberOpinion(
            member=self.name, model=self.model, stance=stance,
            stance_label=label, reasoning=reasoning[:MAX_OPINION],
            confidence=round(confidence, 3),
            simulation=False).to_dict()

    def _abstain(self, cause: str) -> dict[str, Any]:
        self.stance = "abstain"
        return MemberOpinion(
            member=self.name, model=self.model, stance="abstain",
            stance_label=f"{self.role} (abstained)",
            reasoning=f"Abstained: {cause}"[:MAX_OPINION],
            confidence=0.0, simulation=False).to_dict()


def _parse_json_object(text: str) -> dict[str, Any]:
    """Parse model output, tolerating Markdown fences."""
    import json
    import re

    cleaned = (text or "").strip()
    if "```" in cleaned:
        cleaned = re.sub(r"```(?:json)?", "", cleaned).replace(
            "```", "").strip()
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("model response must be a JSON object")
    return data


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
            "simulation": all(bool(opinion.get("simulation", True))
                              for opinion in opinions),
            "members": len(self.members),
            "elapsed_ms": round((time.time() - started) * 1000, 1),
        }
