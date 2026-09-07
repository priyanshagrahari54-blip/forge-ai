"""Browser-initiated rollback tests (A34).

Rollback restores candidate files from the pre-run checkpoint through the
existing policy gate — scoped, approved, and audited.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import (  # noqa: E402
    drive_to_terminal,
    login,
    make_client,
    make_plane,
    make_repo,
    wait_for_status,
)


def _setup(tmp_path, **kwargs):
    plane = make_plane(tmp_path, **kwargs)
    make_repo(Path(plane.projects["demo"].root))
    return plane, make_client(plane)


def test_rollback_completed_task_needs_approval_then_restores(tmp_path):
    plane, client = _setup(tmp_path)
    root = Path(plane.projects["demo"].root)
    (root / "unrelated.txt").write_text("keep me\n")
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]
        final = drive_to_terminal(client, headers, task_id)
        assert final["status"] == "SUCCEEDED"
        assert "export_csv" in (root / "app.py").read_text()

        checkpoints = client.get(
            f"/api/v1/tasks/{task_id}/checkpoints",
            headers=headers).json()["checkpoints"]
        assert checkpoints
        assert checkpoints[0]["status"] == "available"

        # First attempt: approval required, with a scoped request filed.
        first = client.post(f"/api/v1/tasks/{task_id}/rollback",
                            headers=headers, json={})
        assert first.status_code == 409
        assert first.json()["error"]["code"] == "APPROVAL_REQUIRED"
        approval_id = first.json()["error"]["details"]["approval_id"]
        approvals = client.get("/api/v1/approvals",
                               headers=headers).json()["approvals"]
        prompt = [item for item in approvals if item["id"] == approval_id][0]
        assert prompt["agent"] == "cockpit-rollback"
        assert set(prompt["files"]) == {"app.py", "tests/test_csv.py"}
        assert "Restores 2 candidate file(s)" in prompt["consequences"]

        # Approve, then roll back.
        decided = client.post(
            f"/api/v1/approvals/{approval_id}/approve", headers=headers)
        assert decided.status_code == 200
        rolled = client.post(f"/api/v1/tasks/{task_id}/rollback",
                             headers=headers, json={})
        assert rolled.status_code == 200
        assert rolled.json()["status"] == "ROLLED_BACK"

        # Candidate files restored; unrelated work untouched.
        assert (root / "app.py").read_text() == \
            "def health(): return True\n"
        assert not (root / "tests" / "test_csv.py").exists()
        assert (root / "unrelated.txt").read_text() == "keep me\n"

        events = client.get(f"/api/v1/tasks/{task_id}/events?limit=500",
                            headers=headers).json()["events"]
        types = [event["type"] for event in events]
        assert "rollback.requested" in types
        assert "rollback.completed" in types

        # A second rollback has nothing to restore.
        again = client.post(f"/api/v1/tasks/{task_id}/rollback",
                            headers=headers, json={})
        assert again.status_code == 409


def test_rollback_rejected_for_failed_tasks(tmp_path):
    from helpers_a34 import FAILING_PAYLOAD, ScriptedProvider

    plane = make_plane(tmp_path, ScriptedProvider(FAILING_PAYLOAD))
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]
        final = drive_to_terminal(client, headers, task_id, timeout=240.0)
        assert final["status"] == "FAILED"
        # Failed runs already restored their candidates.
        response = client.post(f"/api/v1/tasks/{task_id}/rollback",
                               headers=headers, json={})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ROLLBACK_FAILED"


def test_rollback_unknown_checkpoint_is_404(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]
        final = drive_to_terminal(client, headers, task_id)
        assert final["status"] == "SUCCEEDED"
        response = client.post(
            f"/api/v1/tasks/{task_id}/rollback", headers=headers,
            json={"checkpoint_id": "deadbeefdeadbeef"})
        assert response.status_code == 404
