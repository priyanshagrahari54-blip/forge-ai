"""Autonomous browser end-to-end tests (A34).

A browser-created task travels the real pipeline — queue, supervisor,
planner, model fabric, changeset, policy gate, checkpoint, tests,
review, security, acceptance, git — driven only through the HTTP API.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import (  # noqa: E402
    BlockingProvider,
    approve_all,
    drive_to_terminal,
    login,
    make_client,
    make_plane,
    make_repo,
    task_state,
    wait_for,
    wait_for_status,
)


def _setup(tmp_path, **kwargs):
    plane = make_plane(tmp_path, **kwargs)
    make_repo(Path(plane.projects["demo"].root))
    return plane, make_client(plane)


def _commits(root: Path) -> str:
    return subprocess.run(["git", "log", "--oneline"], cwd=root, text=True,
                          capture_output=True).stdout


def test_browser_csv_export_end_to_end(tmp_path):
    plane, client = _setup(tmp_path)
    root = Path(plane.projects["demo"].root)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        assert created.status_code == 200
        task_id = created.json()["task"]["task_id"]
        final = drive_to_terminal(client, headers, task_id)
        assert final["status"] == "SUCCEEDED"
        assert final["model"] == "m/a34"
        assert final["provider"] == "p"

        # Every major pipeline transition left a real event.
        events = client.get(f"/api/v1/tasks/{task_id}/events?limit=500",
                            headers=headers).json()["events"]
        types = [event["type"] for event in events]
        for expected in ("task.created", "task.started", "run.started",
                         "agent.selected", "model.selected",
                         "change.proposed", "permission.checked",
                         "changes.applied", "tests.executed",
                         "review.completed", "security.completed",
                         "benchmark.completed", "acceptance.completed",
                         "approval.required", "approval.approved",
                         "git.commit", "task.completed",
                         "checkpoint.created", "stage.started"):
            assert expected in types, expected

        # The ordered stage history matches the autonomous lifecycle.
        stages = [event["data"]["stage"] for event in events
                  if event["type"] == "stage.started"]
        for expected in ("planning", "coding", "testing", "review",
                         "security", "benchmark", "acceptance", "commit",
                         "completed"):
            assert expected in stages, stages

        # Backend state is real: behavior, files, commit, report.
        assert "export_csv" in (root / "app.py").read_text()
        assert (root / "tests" / "test_csv.py").exists()
        assert "forge: Add CSV export functionality" in _commits(root)
        report = client.get(f"/api/v1/tasks/{task_id}/report",
                            headers=headers).json()
        assert report["report"]["final_status"] == "COMPLETED"
        assert report["report"]["acceptance"]["accepted"] is True
        verification = client.get(
            f"/api/v1/tasks/{task_id}/verification",
            headers=headers).json()
        assert verification["acceptance"]["accepted"] is True

        # The project view reflects the completed run.
        project = client.get("/api/v1/projects/demo",
                             headers=headers).json()
        assert project["counts"].get("SUCCEEDED", 0) >= 1


def test_pause_and_resume_mid_run(tmp_path):
    provider = BlockingProvider()
    plane, client = _setup(tmp_path, provider=provider)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]
        assert provider.entered.wait(timeout=60.0)
        assert task_state(client, headers, task_id)["status"] == "RUNNING"
        # Request the pause while the model call is blocked, then let the
        # run advance; the pause takes effect at the next stage boundary.
        paused_response = client.post(f"/api/v1/tasks/{task_id}/pause",
                                      headers=headers, json={})
        assert paused_response.status_code == 200
        provider.release.set()
        # The run keeps flowing until the next stage boundary (approvals
        # included), then actually pauses.
        deadline = time.time() + 60.0
        paused_state = None
        while time.time() < deadline:
            approve_all(client, headers)
            current = task_state(client, headers, task_id)
            if current["status"] == "PAUSED":
                paused_state = current
                break
            assert current["status"] not in (
                "SUCCEEDED", "FAILED", "CANCELLED"), current
            time.sleep(0.2)
        assert paused_state is not None, "run never paused"
        events = client.get(f"/api/v1/tasks/{task_id}/events?limit=500",
                            headers=headers).json()["events"]
        assert "task.paused" in [event["type"] for event in events]
        resumed = client.post(f"/api/v1/tasks/{task_id}/resume",
                              headers=headers, json={})
        assert resumed.status_code == 200
        final = drive_to_terminal(client, headers, task_id)
        assert final["status"] == "SUCCEEDED"


def test_cancel_mid_run_rolls_back(tmp_path):
    provider = BlockingProvider()
    plane, client = _setup(tmp_path, provider=provider)
    root = Path(plane.projects["demo"].root)
    (root / "unrelated.txt").write_text("keep me\n")
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]
        assert provider.entered.wait(timeout=60.0)
        cancelled = client.post(f"/api/v1/tasks/{task_id}/cancel",
                                headers=headers, json={})
        assert cancelled.status_code == 200
        provider.release.set()
        final = wait_for_status(client, headers, task_id, "CANCELLED",
                                timeout=120.0)
        assert final["rollback"] is True
        assert (root / "app.py").read_text() == \
            "def health(): return True\n"
        assert not (root / "tests" / "test_csv.py").exists()
        assert (root / "unrelated.txt").read_text() == "keep me\n"
        assert _commits(root).count("initial") == 1
        report = client.get(f"/api/v1/tasks/{task_id}/report",
                            headers=headers).json()
        assert report["report"]["final_status"] == "CANCELLED"


def test_cancel_while_waiting_for_approval(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]
        waiting = wait_for_status(client, headers, task_id,
                                  "WAITING_APPROVAL", timeout=60.0)
        assert waiting["status"] == "WAITING_APPROVAL"
        cancelled = client.post(f"/api/v1/tasks/{task_id}/cancel",
                                headers=headers, json={})
        assert cancelled.status_code == 200
        final = wait_for_status(client, headers, task_id, "CANCELLED",
                                timeout=120.0)
        assert final["rollback"] is True
