"""Regression tests for durable server restart state."""
from __future__ import annotations

from pathlib import Path

from helpers_a34 import (
    ScriptedProvider,
    make_fabric,
    make_plane,
    make_repo,
)

from forge.control.approval_persistence import restore_approval_requests
from forge.control.checkpoint_recovery import restore_available_checkpoints
from forge.control.control_plane import ControlConfig, ControlPlane
from forge.security.approvals import ApprovalRequest, ApprovalStatus
from forge.security.policy import Resource
from forge.tools.checkpoint import CheckpointManager


def test_approval_request_survives_plane_restart(tmp_path):
    plane = make_plane(tmp_path, start=False)
    try:
        restore_approval_requests(plane)
        request = plane.approval_store.submit(ApprovalRequest(
            agent="forge-test", resource=Resource.FILESYSTEM,
            operation="write", scopes=("app.py",), files=("app.py",),
            task_id="task-approval", reason="restart test"))
        plane.approval_store.decide(request.id, True, "alice")
    finally:
        plane.close()

    config = ControlConfig(
        db_path=str(tmp_path / "cockpit.db"),
        projects={"demo": str(tmp_path / "demo")},
        fabric=make_fabric(ScriptedProvider()),
    )
    plane2 = ControlPlane(config)
    try:
        restored = restore_approval_requests(plane2)
        loaded = plane2.approval_store.get_request(request.id)
        assert restored["restored"] >= 1
        assert loaded is not None
        assert loaded.status == ApprovalStatus.APPROVED
        assert loaded.decided_by == "alice"
    finally:
        plane2.close()


def test_checkpoint_snapshot_survives_restart(tmp_path):
    plane = make_plane(tmp_path, start=False)
    try:
        root = Path(plane.projects["demo"].root)
        make_repo(root)
        checkpoint = CheckpointManager(root).create("restart-checkpoint")
        plane._db.execute(
            "INSERT INTO checkpoints (id, run_id, project_id, created_at, "
            "label, snapshot_path, paths_json, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'available')",
            (checkpoint.id, "task-checkpoint", "demo", 1.0, "pre-run",
             str(checkpoint.snapshot), "[]"))
    finally:
        plane.close()

    config = ControlConfig(
        db_path=str(tmp_path / "cockpit.db"),
        projects={"demo": str(root)},
        fabric=make_fabric(ScriptedProvider()),
    )
    plane2 = ControlPlane(config)
    try:
        result = restore_available_checkpoints(plane2)
        assert checkpoint.id in result["restored"]
        restored = plane2._checkpoints[checkpoint.id]
        assert restored.snapshot.is_dir()
        assert restored.files.get("app.py")
    finally:
        plane2.close()
