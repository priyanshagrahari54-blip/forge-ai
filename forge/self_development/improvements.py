from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from forge.self_development.findings import Finding, FindingSeverity


class ImprovementPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class ImprovementCandidate:
    id: str
    title: str
    finding_id: str
    category: str
    priority: str
    description: str
    proposed_improvement: str
    affected_files: list[str] = field(default_factory=list)
    affected_symbols: list[str] = field(default_factory=list)
    estimated_complexity: str = "medium"
    estimated_risk: str = "low"
    score: float = 0.0
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ImprovementCandidate":
        return cls(
            id=data["id"],
            title=data["title"],
            finding_id=data["finding_id"],
            category=data["category"],
            priority=data["priority"],
            description=data.get("description", ""),
            proposed_improvement=data.get("proposed_improvement", ""),
            affected_files=list(data.get("affected_files", [])),
            affected_symbols=list(data.get("affected_symbols", [])),
            estimated_complexity=data.get("estimated_complexity", "medium"),
            estimated_risk=data.get("estimated_risk", "low"),
            score=float(data.get("score", 0.0)),
            evidence=data.get("evidence", ""),
        )


class ImprovementGenerator:
    """Converts structured findings into prioritized, actionable improvement candidates."""

    SEVERITY_WEIGHTS = {
        FindingSeverity.CRITICAL.value: 100.0,
        FindingSeverity.HIGH.value: 75.0,
        FindingSeverity.MEDIUM.value: 50.0,
        FindingSeverity.LOW.value: 25.0,
    }

    COMPLEXITY_PENALTY = {
        "low": 0.0,
        "medium": -10.0,
        "high": -25.0,
    }

    RISK_PENALTY = {
        "low": 0.0,
        "medium": -10.0,
        "high": -25.0,
    }

    def __init__(self, history: list[dict[str, Any]] | None = None) -> None:
        self.history = history or []

    def generate(
        self, findings: list[Finding | dict[str, Any]]
    ) -> list[ImprovementCandidate]:
        candidates: list[ImprovementCandidate] = []

        for idx, item in enumerate(findings, 1):
            finding = (
                item if isinstance(item, Finding) else Finding.from_dict(item)
            )

            cid = f"CANDIDATE-{idx:03d}"
            title = (
                finding.proposed_improvement
                or f"Fix {finding.category} in {', '.join(finding.affected_files)}"
            )

            priority = self._map_priority(finding.severity)
            score = self._compute_score(finding)

            candidate = ImprovementCandidate(
                id=cid,
                title=title,
                finding_id=finding.id,
                category=finding.category,
                priority=priority,
                description=finding.description,
                proposed_improvement=finding.proposed_improvement,
                affected_files=finding.affected_files,
                affected_symbols=finding.affected_symbols,
                estimated_complexity=finding.estimated_complexity,
                estimated_risk=finding.estimated_risk,
                score=score,
                evidence=finding.evidence,
            )
            candidates.append(candidate)

        # Sort candidates by score descending, then ID
        candidates.sort(key=lambda c: (-c.score, c.id))
        return candidates

    def _map_priority(self, severity: str) -> str:
        if severity == FindingSeverity.CRITICAL.value:
            return ImprovementPriority.CRITICAL.value
        elif severity == FindingSeverity.HIGH.value:
            return ImprovementPriority.HIGH.value
        elif severity == FindingSeverity.MEDIUM.value:
            return ImprovementPriority.MEDIUM.value
        return ImprovementPriority.LOW.value

    def _compute_score(self, finding: Finding) -> float:
        base_score = self.SEVERITY_WEIGHTS.get(finding.severity, 25.0)

        complexity_adj = self.COMPLEXITY_PENALTY.get(
            finding.estimated_complexity.lower(), -10.0
        )
        risk_adj = self.RISK_PENALTY.get(finding.estimated_risk.lower(), -10.0)

        # Dependency & affected files impact bonus
        files_bonus = min(len(finding.affected_files) * 5.0, 20.0)

        # Historical failure penalty if category has failed in history
        history_penalty = 0.0
        for entry in self.history:
            if (
                entry.get("candidate", {}).get("category") == finding.category
                and not entry.get("accepted", True)
            ):
                history_penalty -= 15.0

        total_score = base_score + complexity_adj + risk_adj + files_bonus + history_penalty
        return max(total_score, 1.0)
