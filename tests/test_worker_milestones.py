from pathlib import Path

from forge.orchestration.task_milestones import MILESTONES
from forge.orchestration.worker_milestones import WorkerMilestoneController


def test_stage_events_advance_only_from_observed_lifecycle(tmp_path: Path):
    controller = WorkerMilestoneController(str(tmp_path), "task-1")
    snap = controller.snapshot()
    assert snap["passed"] == 0
    assert snap["pending"] == len(MILESTONES)

    controller.observe("stage.started", {"stage": "planning"})
    assert controller.snapshot()["running"] == 1

    controller.observe("stage.started", {"stage": "coding"})
    states = {item["id"]: item["status"] for item in controller.runner.snapshot() and []}
    assert controller.runner.states["planning"].status == "passed"
    assert controller.runner.states["coding"].status == "running"


def test_terminal_completion_is_persisted(tmp_path: Path):
    controller = WorkerMilestoneController(str(tmp_path), "task-2")
    controller.observe("stage.started", {"stage": "acceptance"})
    controller.observe("task.completed", {})
    assert controller.runner.states["acceptance"].status == "passed"
    assert controller.runner.states["completed"].status == "passed"

    state_file = tmp_path / ".forge" / "auto-milestones" / "task-2.json"
    assert state_file.exists()


def test_failed_task_does_not_mark_future_milestones(tmp_path: Path):
    controller = WorkerMilestoneController(str(tmp_path), "task-3")
    controller.observe("stage.started", {"stage": "coding"})
    controller.observe("task.failed", {"error": "compile error"})
    assert controller.runner.states["coding"].status == "failed"
    assert controller.runner.states["testing"].status == "pending"
