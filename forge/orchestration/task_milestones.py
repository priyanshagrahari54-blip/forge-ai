"""Durable task milestone projection for the Forge cockpit."""
from __future__ import annotations

from typing import Any, Iterable

MILESTONES = (
    "planning", "coding", "testing", "debugging", "review", "security",
    "benchmark", "acceptance", "checkpoint", "commit", "completed",
)


def _event_value(event: Any, key: str, default: Any = None) -> Any:
    if isinstance(event, dict):
        return event.get(key, default)
    return getattr(event, key, default)


def project_milestones(events: Iterable[Any], *, task_status: str = "") -> dict[str, Any]:
    """Project observed milestone state from the durable event stream.

    Missing stages remain pending. This is intentionally a read projection,
    not a simulated progress generator.
    """
    states = {name: "pending" for name in MILESTONES}
    attempts = {name: 0 for name in MILESTONES}
    observed = 0

    def mark(name: str, status: str) -> None:
        if name in states:
            states[name] = status

    for event in events:
        event_type = str(_event_value(event, "type", "") or "")
        data = _event_value(event, "data", {})
        if not isinstance(data, dict):
            data = {}
        stage = str(data.get("stage", "") or "").lower().replace(" ", "_")
        changed = False
        if event_type in {"stage.started", "stage.running"} and stage in states:
            mark(stage, "running"); changed = True
        elif event_type in {"stage.completed", "stage.passed", "stage.finished"} and stage in states:
            mark(stage, "passed"); changed = True
        elif event_type == "stage.failed" and stage in states:
            mark(stage, "failed"); changed = True
        elif event_type == "checkpoint.created":
            mark("checkpoint", "passed"); changed = True
        elif event_type == "task.completed":
            mark("acceptance", "passed"); mark("completed", "passed"); changed = True
        elif event_type in {"task.failed", "task.cancelled"}:
            current = next((n for n in reversed(MILESTONES) if states[n] == "running"), None)
            if current:
                mark(current, "failed" if event_type == "task.failed" else "cancelled")
                changed = True
        if changed:
            observed += 1
        if (event_type.endswith(".started") or event_type == "task.started") and stage in attempts:
            attempts[stage] += 1

    passed = sum(v == "passed" for v in states.values())
    running = sum(v == "running" for v in states.values())
    failed = sum(v in {"failed", "cancelled"} for v in states.values())
    return {
        "source": "durable-event-projection",
        "total": len(MILESTONES), "passed": passed, "running": running,
        "failed": failed,
        "pending": len(MILESTONES) - passed - running - failed,
        "task_status": task_status,
        "milestones": [{"id": name, "status": states[name], "attempts": attempts[name]}
                       for name in MILESTONES],
        "observed_events": observed,
    }
