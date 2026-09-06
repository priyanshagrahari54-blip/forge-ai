from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable


def default_timestamp() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class MetricRecord:
    """One measured execution event.

    ``duration_ms`` is wall-clock time; all other fields are recorded
    as-is. ``affected_files`` keeps the context footprint explicit so
    performance can be correlated with repository scope.
    """

    task_id: str
    stage: str
    agent: str = ""
    status: str = "success"
    duration_ms: float = 0.0
    attempts: int = 0
    retries: int = 0
    affected_files: tuple[str, ...] = ()
    checkpoint: str = ""
    model: str = ""
    timestamp: str = ""


class MetricsRecorder:
    """Deterministic, bounded recorder for Forge execution metrics.

    The clock is injectable so tests stay reproducible, but the default
    is a UTC ``datetime`` timestamp.
    """

    def __init__(
        self,
        clock: Callable[[], str] | None = None,
        max_records: int = 10000,
    ) -> None:
        self.clock = clock or default_timestamp
        self.max_records = max_records
        self._records: list[MetricRecord] = []

    def record(
        self,
        task_id: str,
        stage: str,
        *,
        agent: str = "",
        status: str = "success",
        duration_ms: float = 0.0,
        attempts: int = 0,
        retries: int = 0,
        affected_files: tuple[str, ...] = (),
        checkpoint: str = "",
        model: str = "",
    ) -> MetricRecord:
        record = MetricRecord(
            task_id=task_id,
            stage=stage,
            agent=agent,
            status=status,
            duration_ms=duration_ms,
            attempts=attempts,
            retries=retries,
            affected_files=tuple(affected_files),
            checkpoint=checkpoint,
            model=model,
            timestamp=self.clock(),
        )

        self._records.append(record)

        if len(self._records) > self.max_records:
            self._records = self._records[-self.max_records:]

        return record

    @property
    def records(self) -> tuple[MetricRecord, ...]:
        return tuple(self._records)

    def for_task(self, task_id: str) -> tuple[MetricRecord, ...]:
        return tuple(
            record
            for record in self._records
            if record.task_id == task_id
        )

    def for_agent(self, agent: str) -> tuple[MetricRecord, ...]:
        return tuple(
            record
            for record in self._records
            if record.agent == agent
        )

    def by_stage(self, stage: str) -> tuple[MetricRecord, ...]:
        return tuple(
            record
            for record in self._records
            if record.stage == stage
        )

    def summary(self) -> dict:
        """Return a compact, deterministic performance summary."""
        count = len(self._records)

        durations = [
            record.duration_ms
            for record in self._records
            if record.duration_ms >= 0
        ]

        status_counts: dict[str, int] = {}
        stage_counts: dict[str, int] = {}
        agent_durations: dict[str, list[float]] = {}

        for record in self._records:
            status_counts[record.status] = (
                status_counts.get(record.status, 0) + 1
            )
            stage_counts[record.stage] = (
                stage_counts.get(record.stage, 0) + 1
            )
            if record.duration_ms >= 0:
                agent_durations.setdefault(record.agent, []).append(
                    record.duration_ms
                )

        return {
            "count": count,
            "successes": status_counts.get("success", 0),
            "failures": status_counts.get("failure", 0),
            "mean_duration_ms": (
                sum(durations) / len(durations) if durations else 0.0
            ),
            "total_duration_ms": sum(durations),
            "by_status": status_counts,
            "by_stage": stage_counts,
            "mean_duration_ms_by_agent": {
                agent: (
                    sum(times) / len(times) if times else 0.0
                )
                for agent, times in sorted(agent_durations.items())
            },
        }

    def to_dict(self) -> dict:
        return {
            "records": [
                {
                    "task_id": record.task_id,
                    "stage": record.stage,
                    "agent": record.agent,
                    "status": record.status,
                    "duration_ms": record.duration_ms,
                    "attempts": record.attempts,
                    "retries": record.retries,
                    "affected_files": list(record.affected_files),
                    "checkpoint": record.checkpoint,
                    "model": record.model,
                    "timestamp": record.timestamp,
                }
                for record in self._records
            ]
        }

    def clear(self) -> None:
        self._records.clear()