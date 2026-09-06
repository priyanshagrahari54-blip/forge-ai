import hashlib
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, List

from forge.self_development.findings import Finding, FindingSeverity


class CandidateClass(str, Enum):
    PERFORMANCE_IMPROVEMENT = "PERFORMANCE_IMPROVEMENT"
    TEST_IMPROVEMENT = "TEST_IMPROVEMENT"
    SECURITY_IMPROVEMENT = "SECURITY_IMPROVEMENT"
    QUALITY_IMPROVEMENT = "QUALITY_IMPROVEMENT"
    BUG_FIX = "BUG_FIX"
    MAINTENANCE = "MAINTENANCE"


class CandidateStatus(str, Enum):
    ATTEMPTED = "ATTEMPTED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    ROLLED_BACK = "ROLLED_BACK"
    SUPERSEDED = "SUPERSEDED"


class ImprovementPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class ImprovementCandidate:
    id: str
    candidate_hash: str
    title: str
    finding_id: str
    category: str
    candidate_class: str
    priority: str
    description: str
    proposed_improvement: str
    affected_files: List[str] = field(default_factory=list)
    affected_symbols: List[str] = field(default_factory=list)
    estimated_complexity: str = "medium"
    estimated_risk: str = "low"
    score: float = 0.0
    evidence: str = ""
    status: str = CandidateStatus.ATTEMPTED.value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ImprovementCandidate":
        return cls(
            id=data["id"],
            candidate_hash=data.get("candidate_hash", ""),
            title=data["title"],
            finding_id=data["finding_id"],
            category=data["category"],
            candidate_class=data.get("candidate_class", CandidateClass.MAINTENANCE.value),
            priority=data["priority"],
            description=data.get("description", ""),
            proposed_improvement=data.get("proposed_improvement", ""),
            affected_files=list(data.get("affected_files", [])),
            affected_symbols=list(data.get("affected_symbols", [])),
            estimated_complexity=data.get("estimated_complexity", "medium"),
            estimated_risk=data.get("estimated_risk", "low"),
            score=float(data.get("score", 0.0)),
            evidence=data.get("evidence", ""),
            status=data.get("status", CandidateStatus.ATTEMPTED.value),
        )


class ImprovementGenerator:
    """Converts structured findings into prioritized, actionable improvement candidates with stable SHA-256 IDs."""

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

    def __init__(self, history: List[dict[str, Any]] | None = None) -> None:
        self.history = history or []

    def generate(
        self, findings: List[Finding | dict[str, Any]]
    ) -> List[ImprovementCandidate]:
        candidates: List[ImprovementCandidate] = []

        for idx, item in enumerate(findings, 1):
            finding = (
                item if isinstance(item, Finding) else Finding.from_dict(item)
            )

            # Compute stable SHA-256 hash for candidate identity
            identity_str = (
                f"{finding.category}:{finding.description}:"
                f"{','.join(sorted(finding.affected_files))}:"
                f"{','.join(sorted(finding.affected_symbols))}:"
                f"{finding.proposed_improvement}"
            )
            cand_hash = hashlib.sha256(identity_str.encode("utf-8")).hexdigest()[:12]
            cid = f"CANDIDATE-{cand_hash}"

            cand_class = self._determine_candidate_class(finding)
            title = (
                finding.proposed_improvement
                or f"Fix {finding.category} in {', '.join(finding.affected_files)}"
            )

            priority = self._map_priority(finding.severity)
            score = self._compute_score(finding, cand_hash)

            candidate = ImprovementCandidate(
                id=cid,
                candidate_hash=cand_hash,
                title=title,
                finding_id=finding.id,
                category=finding.category,
                candidate_class=cand_class,
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

        candidates.sort(key=lambda c: (-c.score, c.id))
        return candidates

    def _determine_candidate_class(self, finding: Finding) -> str:
        cat = finding.category.lower()
        if cat == "security":
            return CandidateClass.SECURITY_IMPROVEMENT.value
        elif cat == "test_health":
            return CandidateClass.TEST_IMPROVEMENT.value
        elif cat == "performance":
            return CandidateClass.PERFORMANCE_IMPROVEMENT.value
        elif cat in ("todo_fixme", "incomplete_integration"):
            return CandidateClass.QUALITY_IMPROVEMENT.value
        elif cat == "failure_pattern":
            return CandidateClass.BUG_FIX.value
        return CandidateClass.MAINTENANCE.value

    def _map_priority(self, severity: str) -> str:
        if severity == FindingSeverity.CRITICAL.value:
            return ImprovementPriority.CRITICAL.value
        elif severity == FindingSeverity.HIGH.value:
            return ImprovementPriority.HIGH.value
        elif severity == FindingSeverity.MEDIUM.value:
            return ImprovementPriority.MEDIUM.value
        return ImprovementPriority.LOW.value

    def _compute_score(self, finding: Finding, cand_hash: str) -> float:
        base_score = self.SEVERITY_WEIGHTS.get(finding.severity, 25.0)

        complexity_adj = self.COMPLEXITY_PENALTY.get(
            finding.estimated_complexity.lower(), -10.0
        )
        risk_adj = self.RISK_PENALTY.get(finding.estimated_risk.lower(), -10.0)
        files_bonus = min(len(finding.affected_files) * 5.0, 20.0)

        # Check history for repeated candidate failures
        history_penalty = 0.0
        for entry in self.history:
            entry_cand = entry.get("candidate", {})
            if entry_cand.get("candidate_hash") == cand_hash:
                if not entry.get("accepted", True):
                    history_penalty -= 30.0  # Penalize repeatedly failed candidates heavily

        total_score = base_score + complexity_adj + risk_adj + files_bonus + history_penalty
        return max(total_score, 1.0)
