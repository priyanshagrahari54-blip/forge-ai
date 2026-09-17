from pathlib import Path

from forge.orchestration.auto_milestones import MilestoneSpec
from forge.server.milestone_bridge import TaskMilestoneBridge


class FakeServer:
    class Config:
        db_path = ".forge/test-server/server.db"

    config = Config()

    def __init__(self):
        self.events = []
        self.logs = []

    def emit(self, task_id, project_id, event_type, data):
        self.events.append((task_id, project_id, event_type, data))

    def log(self, task_id, project_id, message, **kwargs):
        self.logs.append((task_id, project_id, message, kwargs))


def test_bridge_uses_task_scoped_persistent_state(tmp_path):
    server = FakeServer()
    server.config.db_path = str(tmp_path / "server.db")
    bridge = TaskMilestoneBridge(server, "task-123", "project-1")

    runner = bridge.runner([
        MilestoneSpec("plan", "Plan"),
        MilestoneSpec("build", "Build", depends_on=("plan",)),
    ])

    assert runner.state_path == Path(tmp_path) / "milestones" / "task-123.json"


def test_bridge_emits_milestone_snapshot_and_checkpoint(tmp_path):
    server = FakeServer()
    server.config.db_path = str(tmp_path / "server.db")
    bridge = TaskMilestoneBridge(server, "task-123", "project-1")
    runner = bridge.runner([MilestoneSpec("plan", "Plan")])

    result = runner.run(lambda spec, state: {"ok": True})
    bridge.emit_snapshot(runner)
    bridge.log_progress(runner)

    assert result.success
    assert any(event[2] == "milestone.checkpoint" for event in server.events)
    assert any(event[2] == "milestone.snapshot" for event in server.events)
    assert server.logs
