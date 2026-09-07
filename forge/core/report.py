"""Structured autonomous-task report (A32.14).

Every autonomous task produces a traceable record with the fields below. Raw
prompt/response content and credentials are never stored — only lengths and
identifiers — so reports can be persisted or shipped safely.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskReport:
    task_id: str = ""
    trace_id: str = ""
    requirement: str = ""
    final_status: str = "PENDING"
    stages: list[str] = field(default_factory=list)

    agent: str = ""
    model: str = ""
    provider: str = ""
    context_fingerprint: str = ""

    files_read: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    commands_run: list[list[str]] = field(default_factory=list)
    tests_run: int = 0

    test_result: dict[str, Any] = field(default_factory=dict)
    review_result: dict[str, Any] = field(default_factory=dict)
    security_result: dict[str, Any] = field(default_factory=dict)
    build_result: dict[str, Any] = field(default_factory=dict)
    acceptance: dict[str, Any] = field(default_factory=dict)

    checkpoint_id: str = ""
    retries: int = 0
    duration_seconds: float = 0.0
    error: str = ""
    rollback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "trace_id": self.trace_id,
            "requirement": self.requirement,
            "final_status": self.final_status,
            "stages": list(self.stages),
            "agent": self.agent,
            "model": self.model,
            "provider": self.provider,
            "context_fingerprint": self.context_fingerprint,
            "files_read": list(self.files_read),
            "files_changed": list(self.files_changed),
            "commands_run": [list(cmd) for cmd in self.commands_run],
            "tests_run": self.tests_run,
            "test_result": dict(self.test_result),
            "review_result": dict(self.review_result),
            "security_result": dict(self.security_result),
            "build_result": dict(self.build_result),
            "acceptance": dict(self.acceptance),
            "checkpoint_id": self.checkpoint_id,
            "retries": self.retries,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
            "rollback": self.rollback,
        }
