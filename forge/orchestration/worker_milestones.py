"""Worker-side bridge between real Supervisor lifecycle events and milestones.

This adapter observes the existing execution stream; it never executes work on
its own and never marks a milestone complete without an observed lifecycle
boundary.  The Supervisor remains the execution authority.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from forge.orchestration.auto_milestones import (
    AutoMilestoneRunner,
    MilestoneSpec,
    MilestoneStatus,
)
from forge.orchestration.task_milestones import MILESTONES


def default_specs() -> tuple[MilestoneSpec, ...]:
    """Build the canonical task milestone DAG in execution order."""
    return tuple(
        MilestoneSpec(
            id=name,
            title=name.replace("_", " ").title(),
            depends_on=(MILESTONES[index - 1],) if index else (),
            auto_continue=True,
        )
        for index, name in enumerate(MILESTONES)
    )


class WorkerMilestoneController:
    """Observe real worker/Supervisor events and persist milestone state."""

    def __init__(self, project_root: str, task_id: str) -> None:
        safe_id = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(task_id))
        state_path = Path(project_root) / ".forge" / "auto-milestones" / f"{safe_id}.json"
        self.runner = AutoMilestoneRunner(default_specs(), state_path=state_path)

    def _set(self, milestone_id: str, status: MilestoneStatus, detail: str = "") -> None:
        spec = self.runner.specs.get(milestone_id)
        if spec is None:
            return
        state = self.runner.states[milestone_id]
        # Do not regress a passed milestone on duplicate/replayed events.
        if state.status == MilestoneStatus.PASSED.value and status != MilestoneStatus.PASSED:
            return
        if status == MilestoneStatus.RUNNING and state.status == MilestoneStatus.PENDING.value:
            state.attempts += 1
        state.status = status.value
        state.updated_at = __import__("time").time()
        if detail and status in {MilestoneStatus.FAILED, MilestoneStatus.BLOCKED}:
            state.last_error = detail[:2000]
        self.runner._emit(milestone_id, status.value, state.status, state.attempts, detail)
        self.runner.save()

    def observe(self, event_type: str, data: Dict[str, Any] | None = None) -> None:
        """Consume one actual server/Supervisor event.

        Completion is inferred only at the boundary where the next real stage
        begins or the task emits a terminal event. Replayed events are safe.
        """
        data = data if isinstance(data, dict) else {}
        event_type = str(event_type or "")
        stage = str(data.get("stage", "") or "").lower().replace(" ", "_")

        if event_type == "stage.started" and stage in self.runner.specs:
            index = MILESTONES.index(stage)
            if index:
                previous = MILESTONES[index - 1]
                if self.runner.states[previous].status == MilestoneStatus.RUNNING.value:
                    self._set(previous, MilestoneStatus.PASSED)
            self._set(stage, MilestoneStatus.RUNNING)
            return

        if event_type in {"stage.failed", "task.failed", "task.cancelled"}:
            current = next(
                (name for name in reversed(MILESTONES)
                 if self.runner.states[name].status == MilestoneStatus.RUNNING.value),
                None,
            )
            if current:
                status = (MilestoneStatus.FAILED if event_type == "task.failed"
                          else MilestoneStatus.FAILED)
                self._set(current, status, str(data.get("error", "") or event_type))
            return

        if event_type == "checkpoint.created":
            self._set("checkpoint", MilestoneStatus.PASSED)
            return

        if event_type == "task.completed":
            current = next(
                (name for name in reversed(MILESTONES[:-1])
                 if self.runner.states[name].status == MilestoneStatus.RUNNING.value),
                None,
            )
            if current:
                self._set(current, MilestoneStatus.PASSED)
            self._set("completed", MilestoneStatus.PASSED)

    def snapshot(self) -> dict[str, Any]:
        return self.runner.snapshot()
