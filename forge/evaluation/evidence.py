"""Evidence ledger for truthful build/test/security/acceptance decisions."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Iterable

class EvidenceStatus(str, Enum):
    PASS="pass"; FAIL="fail"; UNKNOWN="unknown"

@dataclass(frozen=True)
class Evidence:
    kind: str
    status: EvidenceStatus
    source: str
    message: str = ""
    artifact: str = ""
    metric: str = ""
    value: str = ""

@dataclass
class EvidenceLedger:
    entries: list[Evidence]

    def add(self, evidence: Evidence) -> None:
        self.entries.append(evidence)

    def status(self) -> EvidenceStatus:
        if any(e.status is EvidenceStatus.FAIL for e in self.entries): return EvidenceStatus.FAIL
        if not self.entries or any(e.status is EvidenceStatus.UNKNOWN for e in self.entries): return EvidenceStatus.UNKNOWN
        return EvidenceStatus.PASS

    def by_kind(self, kind: str) -> list[Evidence]:
        return [e for e in self.entries if e.kind == kind]

    def to_dict(self) -> dict:
        return {"status":self.status().value,"entries":[asdict(e) for e in self.entries]}

    @classmethod
    def from_entries(cls, entries: Iterable[Evidence]=()):
        return cls(list(entries))
