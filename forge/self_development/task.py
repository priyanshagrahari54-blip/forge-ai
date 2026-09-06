from dataclasses import asdict, dataclass, field
from typing import Any, List


@dataclass
class SelfDevelopmentTask:
    candidate_id: str
    candidate_hash: str
    objective: str
    category: str
    affected_files: List[str] = field(default_factory=list)
    evidence: str = ""
    constraints: List[str] = field(default_factory=list)
    acceptance_criteria: List[str] = field(default_factory=list)
    risk: str = "low"
    expected_improvement: str = ""
    instructions: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_candidate(cls, candidate_obj: Any) -> "SelfDevelopmentTask":
        c = candidate_obj
        constraints = [
            "Modify only necessary files",
            "Preserve existing behavior and tests",
            "Add or update tests for new changes",
            "Do not weaken security or expose credentials",
            "Do not modify unrelated files or .forge/self/history",
            "Explain the proposed change clearly",
            "Run verification after editing",
        ]
        acceptance_criteria = [
            "All unit and integration tests pass",
            "No new security findings or vulnerabilities introduced",
            "Python compilation and build checks pass",
        ]

        instructions = (
            f"Self-Development Task: {c.title}\n"
            f"Candidate ID: {c.id}\n"
            f"Category: {c.category}\n"
            f"Affected Files: {', '.join(c.affected_files)}\n"
            f"Objective: {c.proposed_improvement or c.description}\n"
            f"Evidence: {c.evidence}\n"
            f"Constraints:\n" + "\n".join(f"- {cons}" for cons in constraints)
        )

        return cls(
            candidate_id=c.id,
            candidate_hash=getattr(c, "candidate_hash", ""),
            objective=c.proposed_improvement or c.description,
            category=c.category,
            affected_files=c.affected_files,
            evidence=c.evidence,
            constraints=constraints,
            acceptance_criteria=acceptance_criteria,
            risk=c.estimated_risk,
            expected_improvement=c.proposed_improvement,
            instructions=instructions,
        )
