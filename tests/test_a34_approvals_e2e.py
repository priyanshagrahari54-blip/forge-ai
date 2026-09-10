"""Interactive approval end-to-end tests (A34).

Real supervisor runs against an isolated repository: the worker blocks
for browser approval mid-run, and the test decides through the API.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import (  # noqa: E402
    drive_to_terminal,
    login,
    make_client,
    make_plane,
    make_repo,
    wait_for,
    wait_for_status,
)


def _setup(tmp_path, **kwargs):
    plane = make_plane(tmp_path, **kwargs)
    make_repo(Path(plane.projects["demo"].root))
    return plane, make_client(plane)


def _pending(client, headers):
    return client.get("/api/v1/approvals", headers=headers).json()[
        "approvals"]


def _commits(root: Path) -> str:
    return subprocess.run(["git", "log", "--oneline"], cwd=root, text=True,
                          capture_output=True).stdout


def test_approve_all_completes_run_with_commit(tmp_path):
    plane, client = _setup(tmp_path)
    root = Path(plane.projects["demo"].root)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]
        final = drive_to_terminal(client, headers, task_id)
        assert final["status"] == "SUCCEEDED"
        # Real backend state: files written, tests pass, commit exists.
        assert "export_csv" in (root / "app.py").read_text()
        assert (root / "tests" / "test_csv.py").exists()
        assert _commits(root).count("\n") >= 2 - 1  # initial + forge
        assert "forge: Add CSV export functionality" in _commits(root)
        events = client.get(f"/api/v1/tasks/{task_id}/events?limit=500",
                            headers=headers).json()["events"]
        types = [event["type"] for event in events]
        assert "approval.required" in types
        assert "approval.approved" in types
        assert "git.commit" in types
        assert "task.completed" in types
        # The approval prompt carried the full WHAT/WHY.
        required = [event for event in events
                    if event["type"] == "approval.required"][0]
        for key in ("agent", "operation", "paths", "risk", "reason",
                    "expires_at"):
            assert key in required["data"], key


def test_deny_fails_closed_with_rollback(tmp_path):
    plane, client = _setup(tmp_path)
    root = Path(plane.projects["demo"].root)
    (root / "unrelated.txt").write_text("do not touch\n")
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]

        def pending():
            approvals = _pending(client, headers)
            return approvals[0]["id"] if approvals else None

        approval_id = wait_for(pending, timeout=60.0)
        denied = client.post(f"/api/v1/approvals/{approval_id}/deny",
                             headers=headers)
        assert denied.status_code == 200
        final = wait_for_status(client, headers, task_id, "FAILED",
                                timeout=120.0)
        assert "approv" in final["error"].lower() or "permit" in final[
            "error"].lower()
        # Nothing applied, nothing committed, unrelated work preserved.
        assert (root / "app.py").read_text() == \
            "def health(): return True\n"
        assert not (root / "tests" / "test_csv.py").exists()
        assert (root / "unrelated.txt").read_text() == "do not touch\n"
        assert _commits(root).count("initial") == 1
        events = client.get(f"/api/v1/tasks/{task_id}/events?limit=500",
                            headers=headers).json()["events"]
        types = [event["type"] for event in events]
        assert "approval.denied" in types
        assert "task.failed" in types
        assert "git.commit" not in types


def test_write_approval_does_not_authorize_commit(tmp_path):
    """Per-operation scoping: approving files must not approve the commit."""
    plane, client = _setup(tmp_path)
    root = Path(plane.projects["demo"].root)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]

        def pending():
            return _pending(client, headers)

        # Approve the file writes, deny the commit.
        write_done = wait_for(
            lambda: [item for item in pending()
                     if item["resource"] == "filesystem"
                     and item["operation"] == "write"],
            timeout=60.0)
        for item in write_done:
            response = client.post(
                f"/api/v1/approvals/{item['id']}/approve", headers=headers)
            assert response.status_code == 200
        commit_items = wait_for(
            lambda: [item for item in pending()
                     if item["resource"] == "git"
                     and item["operation"] == "commit"],
            timeout=60.0)
        for item in commit_items:
            response = client.post(
                f"/api/v1/approvals/{item['id']}/deny", headers=headers)
            assert response.status_code == 200
        final = wait_for_status(client, headers, task_id, "FAILED",
                                timeout=120.0)
        assert "commit" in final["error"].lower()
        assert _commits(root).count("initial") == 1
        assert (root / "app.py").read_text() == \
            "def health(): return True\n"


def test_approval_expiry_fails_the_run(tmp_path):
    plane, client = _setup(tmp_path, approval_timeout=2.0)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]
        # Never decide: the waiter must give up and fail closed.
        final = wait_for_status(client, headers, task_id, "FAILED",
                                timeout=120.0)
        assert final["error"]
        events = client.get(f"/api/v1/tasks/{task_id}/events?limit=500",
                            headers=headers).json()["events"]
        types = [event["type"] for event in events]
        assert "approval.expired" in types
        assert "task.failed" in types
