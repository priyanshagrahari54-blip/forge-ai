"""Persistent server bridge for automatic milestone orchestration.

The bridge gives each background task its own resumable milestone state and
connects milestone events to the existing Forge event/log stores. It does not
replace the task queue or worker: the worker remains the authority for actual
execution and this component owns progression/checkpoint bookkeeping.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable

from forge.orchestration.auto_milestones import (
    AutoMilestoneRunner,
    MilestoneSpec,
    MilestoneState,
)


class TaskMilestoneBridge:
    """Bind AutoMilestoneRunner to one persistent Forge task."""

    def __init__(self, server: Any, task_id: str, project_id: str) -> None:
        self.server = server
        self.task_id = task_id
        self.project_id = project_id
        root = Path(getattr(server.config, "db_path", ".forge/server/server.db"))
        self.state_path = root.parent / "milestones" / (task_id + ".json")

    def runner(
        self,
        milestones: Iterable[MilestoneSpec],
        *,
        approval_check: Callable[[MilestoneSpec], bool] | None = None,
    ) -> AutoMilestoneRunner:
        def checkpoint(spec: MilestoneSpec, state: MilestoneState, outcome: str) -> None:
            self.server.emit(
                self.task_id,
                self.project_id,
                "milestone.checkpoint",
                {
                    "milestone_id": spec.id,
                    "attempt": state.attempts,
                    "outcome": outcome,
                    "checkpoint": state.checkpoint,
                },
            )

        return AutoMilestoneRunner(
            milestones,
            state_path=self.state_path,
            checkpoint=checkpoint,
            approval_check=approval_check,
        )

    def emit_snapshot(self, runner: AutoMilestoneRunner) -> dict[str, Any]:
        snapshot = runner.snapshot()
        self.server.emit(
            self.task_id,
            self.project_id,
            "milestone.snapshot",
            snapshot,
        )
        return snapshot

    def log_progress(self, runner: AutoMilestoneRunner) -> None:
        snapshot = runner.snapshot()
        self.server.log(
            self.task_id,
            self.project_id,
            "Milestones: %(passed)d passed, %(running)d running, "
            "%(pending)d pending, %(failed)d failed, %(blocked)d blocked."
            % snapshot,
            source="milestone-orchestrator",
        )
