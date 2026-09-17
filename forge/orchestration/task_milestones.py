"""Durable task milestone projection for the Forge cockpit.

This module deliberately projects milestones from persisted task events instead
of inventing progress. The worker/event log remains the source of truth; this
projection is a read model for the UI and APIs.
"""
from __future__ import annotations

from typing import Any, Iterable


MILESTONES = (
    "planning",
    "coding",
    "testing",
    "debugging",
    "review",
    "security",
    "benchmark",
    "acceptance",
    "checkpoint",
    "commit",
    "completed",
)

_TERMINAL = {"task.completed", "task.failed", "task.cancelled"}


def project_milestones(events: Iterable[Any], *, task_status: str = "") -> dict[str, Any]:
    """Build a milestone snapshot from persisted events.

    Only observed events can advance a milestone. Unknown/missing stages stay
    pending, so the projection never presents simulated work as real progress.
    """
    states = {name: "pending" for name in MILESTONES}
    attempts = {name: 0 for name in MILESTONES}
    observed: list[dict[str, Any]] = []

    def mark(name: str, status: str) -> None:
        if name in states:
            states[name] = status
            observed.append({"milestone": name, "status": status})

    for event in events:
        event_type = str(getattr(event, "type", "") or "")
        data = getattr(event, "data", None)
        if not isinstance(data, dict):
            data = {}
        stage = str(data.get("stage", "") or "").lower().replace(" ", "_")
        if event_type in {"stage.started", "stage.running"} and stage in states:
            mark(stage, "running")
        elif event_type in {"stage.completed", "stage.passed", "stage.finished"} and stage in states:
            mark(stage, "passed")
        elif event_type in {"stage.failed"} and stage in states:
            mark(stage, "failed")
        elif event_type == "checkpoint.created":
            mark("checkpoint", "passed")
        elif event_type == "task.completed":
            mark("acceptance", "passed")
            mark("completed", "passed")
        elif event_type == "task.failed":
            current = next((n for n in reversed(MILESTONES) if states[n] == "running"), None)
            if current:
                mark(current, "failed")
        elif event_type == "task.cancelled":
            current = next((n for n in reversed(MILESTONES) if states[n] == "running"), None)
            if current:
                mark(current, "cancelled")

        if event_type.endswith(".started") or event_type == "task.started":
            if stage in attempts:
                attempts[stage] += 1

    passed = sum(value == "passed" for value in states.values())
    running = sum(value == "running" for value in states.values())
    failed = sum(value in {"failed", "cancelled"} for value in states.values())
    return {
        "source": "durable-event-projection",
        "total": len(MILESTONES),
        "passed": passed,
        "running": running,
        "failed": failed,
        "pending": len(MILESTONES) - passed - running - failed,
        "task_status": task_status,
        "milestones": [
            {"id": name, "status": states[name], "attempts": attempts[name]}
            for name in MILESTONES
        ],
        "observed_events": len(observed),
    }
