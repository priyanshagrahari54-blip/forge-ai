"""Structured autonomous-task report (A32.11 / A32.14).

Every autonomous task produces a traceable record with the fields below: a
redacted requirement, stage history, per-stage measured timings, model
latency/token metadata (when the provider supply it), and an ordered event
log covering task start, agent/model selection, proposed and applied changes,
permission decisions, test executions and failures, repairs, gate results,
acceptance, rollback, and commit.

Raw prompt/response content and credentials are never stored — ``to_dict()``
redacts secret-looking values throughout — so reports can be persisted or
shipped safely.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

REDACTED = "***REDACTED***"

_REDACT_PATTERNS = (
    re.compile(r"(?:api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]{4,}['\"]?", re.I),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[^-]*"
               r"(?:-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)?", re.S),
    re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    re.compile(r"(?:postgres|mysql|mongodb(?:\+srv)?)://[^\s'\"]+", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{20,}"),
)


def redact_text(text: str) -> str:
    """Mask secret-looking values in a string."""
    masked = text
    for pattern in _REDACT_PATTERNS:
        masked = pattern.sub(REDACTED, masked)
    return masked


def redact(value: Any) -> Any:
    """Recursively redact secret-looking values in report payloads."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


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

    #: Ordered observability events: ``{name, t, details}`` where ``t`` is
    #: seconds since the run started.
    events: list[dict[str, Any]] = field(default_factory=list)
    #: Measured per-phase durations in seconds (plus ``total``).
    timings: dict[str, float] = field(default_factory=dict)
    #: Summed model latency in seconds across routing history (0 when none).
    model_latency_seconds: float = 0.0
    #: Summed token counts when providers report them, else ``None``.
    input_tokens: int | None = None
    output_tokens: int | None = None
    #: Permission mode the run executed under (safe/assisted/autonomous/locked).
    mode: str = ""

    def record_event(self, name: str, elapsed: float,
                     details: dict[str, Any] | None = None) -> None:
        self.events.append({"name": name, "t": elapsed, "details": details or {}})

    def to_dict(self) -> dict[str, Any]:
        payload = {
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
            "events": [dict(item) for item in self.events],
            "timings": dict(self.timings),
            "model_latency_seconds": self.model_latency_seconds,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "mode": self.mode,
        }
        return redact(payload)
