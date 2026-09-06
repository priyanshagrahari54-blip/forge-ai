import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, List, Optional


@dataclass
class HistoryRecord:
    timestamp: float
    candidate_id: str
    candidate_hash: str
    candidate_class: str
    title: str
    baseline_commit: str
    checkpoint_id: str
    model_used: str = "default"
    agents_used: List[str] = field(default_factory=list)
    context_fingerprint: str = ""
    files_changed: List[str] = field(default_factory=list)
    diff_summary: str = ""
    test_results: dict = field(default_factory=dict)
    security_results: dict = field(default_factory=dict)
    build_results: dict = field(default_factory=dict)
    benchmark_results: dict = field(default_factory=dict)
    attempts: List[dict] = field(default_factory=list)
    accepted: bool = False
    accepted_because: Optional[str] = None
    rejected_because: Optional[str] = None
    final_commit: Optional[str] = None
    rollback_status: str = "NOT_NEEDED"
    duration: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HistoryRecord":
        return cls(
            timestamp=float(data.get("timestamp", time.time())),
            candidate_id=data.get("candidate_id", ""),
            candidate_hash=data.get("candidate_hash", ""),
            candidate_class=data.get("candidate_class", "MAINTENANCE"),
            title=data.get("title", ""),
            baseline_commit=data.get("baseline_commit", ""),
            checkpoint_id=data.get("checkpoint_id", ""),
            model_used=data.get("model_used", "default"),
            agents_used=list(data.get("agents_used", [])),
            context_fingerprint=data.get("context_fingerprint", ""),
            files_changed=list(data.get("files_changed", [])),
            diff_summary=data.get("diff_summary", ""),
            test_results=dict(data.get("test_results", {})),
            security_results=dict(data.get("security_results", {})),
            build_results=dict(data.get("build_results", {})),
            benchmark_results=dict(data.get("benchmark_results", {})),
            attempts=list(data.get("attempts", [])),
            accepted=bool(data.get("accepted", False)),
            accepted_because=data.get("accepted_because"),
            rejected_because=data.get("rejected_because"),
            final_commit=data.get("final_commit"),
            rollback_status=data.get("rollback_status", "NOT_NEEDED"),
            duration=float(data.get("duration", 0.0)),
        )


class HistoryStore:
    """Persists and queries self-development execution records under .forge/self/history/."""

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()
        self.history_dir = self.root / ".forge" / "self" / "history"
        self.history_dir.mkdir(parents=True, exist_ok=True)

    def save(self, record: HistoryRecord) -> Path:
        file_name = f"run_{record.candidate_hash}_{int(record.timestamp * 1000)}.json"
        target = self.history_dir / file_name
        target.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
        return target

    def list_records(self) -> List[HistoryRecord]:
        records: List[HistoryRecord] = []
        for file in sorted(self.history_dir.glob("run_*.json")):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
                records.append(HistoryRecord.from_dict(data))
            except Exception:
                continue
        return records

    def is_candidate_repeatedly_failed(
        self, candidate_hash: str, max_failures: int = 2
    ) -> bool:
        failures = 0
        for rec in self.list_records():
            if rec.candidate_hash == candidate_hash and not rec.accepted:
                failures += 1
                if failures >= max_failures:
                    return True
        return False
