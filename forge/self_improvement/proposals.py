"""Improvement proposals (A81).

A proposal is a *structured hypothesis*, not a change. Every proposal must
carry a hypothesis, the evidence ids it rests on, the expected benefit, a
risk assessment, the affected files, and a test plan. :func:`validate_proposal`
rejects anything incomplete, anything without evidence, and anything whose
declared files violate the guardrails — before a candidate is ever built.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import Any, Iterable

from forge.self_improvement.analysis import SelfAnalysisReport, Weakness
from forge.self_improvement.guardrails import Guardrails

RISK_LEVELS = ("low", "medium", "high")
MAX_AFFECTED_FILES = 12
MAX_TEXT = 1000


@dataclass
class ImprovementProposal:
    id: str
    title: str
    hypothesis: str
    evidence_ids: list[str]
    expected_benefit: str
    risk: str
    risk_notes: str
    affected_files: list[str]
    test_plan: list[str]
    weakness_id: str = ""
    category: str = ""
    metric: str = ""
    baseline_value: float | None = None
    target_value: float | None = None
    instructions: str = ""
    created_at: float = field(default_factory=time.time)
    status: str = "proposed"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ImprovementProposal":
        return cls(
            id=str(data["id"]), title=str(data.get("title", "")),
            hypothesis=str(data.get("hypothesis", "")),
            evidence_ids=[str(x) for x in data.get("evidence_ids", [])],
            expected_benefit=str(data.get("expected_benefit", "")),
            risk=str(data.get("risk", "medium")),
            risk_notes=str(data.get("risk_notes", "")),
            affected_files=[str(x) for x in data.get("affected_files", [])],
            test_plan=[str(x) for x in data.get("test_plan", [])],
            weakness_id=str(data.get("weakness_id", "")),
            category=str(data.get("category", "")),
            metric=str(data.get("metric", "")),
            baseline_value=data.get("baseline_value"),
            target_value=data.get("target_value"),
            instructions=str(data.get("instructions", "")),
            created_at=float(data.get("created_at", 0.0) or 0.0),
            status=str(data.get("status", "proposed")),
        )


def validate_proposal(proposal: ImprovementProposal,
                      known_evidence: Iterable[str] | None = None,
                      guardrails: Guardrails | None = None) -> list[str]:
    """Return a list of problems (empty means the proposal is admissible)."""
    problems: list[str] = []
    for name in ("title", "hypothesis", "expected_benefit"):
        value = getattr(proposal, name, "")
        if not str(value).strip():
            problems.append(f"missing {name}")
        elif len(str(value)) > MAX_TEXT:
            problems.append(f"{name} exceeds {MAX_TEXT} characters")
    if not proposal.evidence_ids:
        problems.append("no evidence cited")
    elif known_evidence is not None:
        known = set(known_evidence)
        unknown = [e for e in proposal.evidence_ids if e not in known]
        if unknown:
            problems.append(f"unknown evidence ids: {', '.join(unknown[:5])}")
    if proposal.risk not in RISK_LEVELS:
        problems.append(f"risk must be one of {RISK_LEVELS}")
    if not proposal.affected_files:
        problems.append("no affected files declared")
    elif len(proposal.affected_files) > MAX_AFFECTED_FILES:
        problems.append(f"more than {MAX_AFFECTED_FILES} affected files")
    if not proposal.test_plan:
        problems.append("no test plan")
    rails = guardrails or Guardrails()
    for violation in rails.check_paths(proposal.affected_files):
        problems.append(f"guardrail: {violation['rule']} ({violation['path']})")
    return problems


class ProposalGenerator:
    """Turn ranked weaknesses into structured proposals (deterministic)."""

    def __init__(self, guardrails: Guardrails | None = None,
                 history: Iterable[dict[str, Any]] | None = None) -> None:
        self.guardrails = guardrails or Guardrails()
        self.history = list(history or [])

    def _rejected_before(self, weakness_id: str) -> int:
        return sum(
            1 for entry in self.history
            if entry.get("weakness_id") == weakness_id
            and entry.get("outcome") == "rejected")

    def generate(self, report: SelfAnalysisReport,
                 *, limit: int = 10) -> list[ImprovementProposal]:
        proposals: list[ImprovementProposal] = []
        for weakness in report.weaknesses:
            if not weakness.affected_files:
                continue
            proposal = self._from_weakness(weakness)
            problems = validate_proposal(
                proposal, [e.id for e in report.evidence], self.guardrails)
            if problems:
                proposal.status = "inadmissible: " + "; ".join(problems)
                continue
            if self._rejected_before(weakness.id) >= 2:
                proposal.status = "suppressed: rejected twice before"
                continue
            proposals.append(proposal)
            if len(proposals) >= limit:
                break
        return proposals

    def _from_weakness(self, weakness: Weakness) -> ImprovementProposal:
        identity = "|".join((weakness.category, weakness.metric,
                             *sorted(weakness.affected_files),
                             weakness.suggested_action))
        pid = "PROP-" + sha256(identity.encode("utf-8")).hexdigest()[:12]
        tests = [f"tests/{p.split('/')[-1].replace('.py', '')}" for p in weakness.affected_files]
        test_plan = [
            f"run the full suite; it must not regress ({weakness.metric} baseline "
            f"{weakness.value})",
            "run security verification on the changed files",
            "run architecture checks (no new dependency cycles, no protected files)",
        ] + [f"run targeted tests matching {t}" for t in tests[:3]]
        risk = "low" if weakness.severity in ("low", "medium") else "medium"
        if any(p.startswith(("forge/core/", "forge/models/", "forge/agents/"))
               for p in weakness.affected_files):
            risk = "medium" if risk == "low" else "high"
        return ImprovementProposal(
            id=pid,
            title=weakness.suggested_action[:120] or weakness.title[:120],
            hypothesis=(
                f"{weakness.title}. If we {weakness.suggested_action.rstrip('.')}, "
                f"then {weakness.metric} should improve from {weakness.value} "
                f"toward {weakness.target}."),
            evidence_ids=list(weakness.evidence_ids),
            expected_benefit=(
                f"{weakness.metric}: {weakness.value} -> {weakness.target} "
                f"({weakness.category})"),
            risk=risk,
            risk_notes=weakness.risk_notes,
            affected_files=list(weakness.affected_files),
            test_plan=test_plan,
            weakness_id=weakness.id,
            category=weakness.category,
            metric=weakness.metric,
            baseline_value=weakness.value if isinstance(weakness.value, (int, float)) else None,
            target_value=weakness.target if isinstance(weakness.target, (int, float)) else None,
            instructions=weakness.suggested_action,
        )
