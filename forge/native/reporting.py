"""Final reporting for Native AI runs.

One structured record per run, safe to persist: secret-looking values are
redacted with the same machinery used by A32 task reports
(:func:`forge.core.report.redact`), raw prompts/credentials never enter the
report, and every number in it is measured (stages actually visited, files
actually touched, gates actually executed, retries actually attempted).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from forge.core.report import redact


@dataclass
class NativeRunReport:
    """The engine's final report (also persisted under ``.forge/native``)."""

    run_id: str = ""
    task_id: str = ""
    project: str = ""
    requirement: str = ""
    final_status: str = "PENDING"
    task_class: str = ""
    started_at: float = 0.0
    duration_seconds: float = 0.0

    stages: List[str] = field(default_factory=list)
    plan: Dict[str, Any] = field(default_factory=dict)
    backends: Dict[str, Any] = field(default_factory=dict)
    model_backend: Dict[str, Any] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)

    files_read: List[str] = field(default_factory=list)
    files_changed: List[str] = field(default_factory=list)
    test_runs: List[Dict[str, Any]] = field(default_factory=list)
    debug: Dict[str, Any] = field(default_factory=dict)
    verification: Dict[str, Any] = field(default_factory=dict)
    review: Dict[str, Any] = field(default_factory=dict)
    security: Dict[str, Any] = field(default_factory=dict)
    memory: Dict[str, Any] = field(default_factory=dict)

    retries: int = 0
    rollback: bool = False
    error: str = ""
    #: Capabilities that could not run because they need neural inference.
    skipped_neural: List[str] = field(default_factory=list)
    #: What ran entirely without a model (for the free-first honesty label).
    executed_without_model: List[str] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    #: True when the run record includes captured model output for the
    #: future training dataset (only when dataset capture is explicitly on).
    dataset_captured: bool = False
    #: Captured model output (opt-in only): raw text + provenance of the
    #: proposal that was applied and verified. Feeds the dataset builder.
    model_output: Dict[str, Any] = field(default_factory=dict)

    def record_event(self, name: str, t_offset: float,
                     details: Optional[Dict[str, Any]] = None) -> None:
        self.events.append({"name": name, "t": round(float(t_offset), 4),
                            "details": redact(dict(details or {}))})

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "project": self.project,
            "requirement": self.requirement[:500],
            "final_status": self.final_status,
            "task_class": self.task_class,
            "started_at": self.started_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "stages": list(self.stages),
            "plan": self.plan,
            "backends": self.backends,
            "model_backend": self.model_backend,
            "context": self.context,
            "files_read": list(self.files_read),
            "files_changed": list(self.files_changed),
            "test_runs": list(self.test_runs),
            "debug": self.debug,
            "verification": self.verification,
            "review": self.review,
            "security": self.security,
            "memory": self.memory,
            "retries": self.retries,
            "rollback": self.rollback,
            "error": self.error,
            "skipped_neural": list(self.skipped_neural),
            "executed_without_model": list(self.executed_without_model),
            "events": list(self.events),
            "notes": list(self.notes),
            "dataset_captured": self.dataset_captured,
            "model_output": dict(self.model_output),
        }
        return redact(payload)


def new_run_id() -> str:
    """Sortable, collision-resistant run id (UTC second + process time)."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-%06d" % (
        int((time.time() % 1) * 1_000_000) % 1_000_000)
