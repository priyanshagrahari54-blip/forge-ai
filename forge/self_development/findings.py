from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class FindingCategory(str, Enum):
    TODO_FIXME = "todo_fixme"
    TEST_HEALTH = "test_health"
    SECURITY = "security"
    PERFORMANCE = "performance"
    ARCHITECTURE = "architecture"
    INCOMPLETE_INTEGRATION = "incomplete_integration"
    FAILURE_PATTERN = "failure_pattern"


class FindingSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class Finding:
    id: str
    category: str
    severity: str
    evidence: str
    affected_files: list[str] = field(default_factory=list)
    affected_symbols: list[str] = field(default_factory=list)
    description: str = ""
    proposed_improvement: str = ""
    estimated_complexity: str = "medium"
    estimated_risk: str = "low"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Finding":
        return cls(
            id=data["id"],
            category=data["category"],
            severity=data["severity"],
            evidence=data["evidence"],
            affected_files=list(data.get("affected_files", [])),
            affected_symbols=list(data.get("affected_symbols", [])),
            description=data.get("description", ""),
            proposed_improvement=data.get("proposed_improvement", ""),
            estimated_complexity=data.get("estimated_complexity", "medium"),
            estimated_risk=data.get("estimated_risk", "low"),
        )
