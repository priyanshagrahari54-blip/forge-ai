"""Cockpit API tests (A34): health, sessions, tasks, views, errors."""
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
    task_state,
    wait_for_status,
)


def _setup(tmp_path, **kwargs):
    plane = make_plane(tmp_path, **kwargs)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    return plane, client


def test_health_public_and_labels_local_dev(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["auth_mode"] == "local-dev"
        assert "request_id" not in payload
        assert response.headers["X-Request-ID"]


def test_session_lifecycle(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        me = client.get("/api/v1/sessions/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["session"]["actor"] == "alice"
        assert me.json()["session"]["project_id"] == "demo"
        assert "token" not in me.json()["session"]
        revoked = client.delete("/api/v1/sessions/me", headers=headers)
        assert revoked.status_code == 200
        gone = client.get("/api/v1/sessions/me", headers=headers)
        assert gone.status_code == 401
        assert gone.json()["error"]["code"] == "AUTH_REQUIRED"


def test_session_rejects_unknown_project_and_bad_actor(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        bad_project = client.post("/api/v1/sessions", json={
            "actor": "alice", "project_id": "nope"})
        assert bad_project.status_code == 404
        assert bad_project.json()["error"]["code"] == "PROJECT_NOT_FOUND"
        bad_actor = client.post("/api/v1/sessions", json={
            "actor": "not an actor!!!", "project_id": "demo"})
        assert bad_actor.status_code == 400
        assert bad_actor.json()["error"]["code"] == "INVALID_REQUEST"


def test_task_submit_and_read(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        assert created.status_code == 200
        task = created.json()["task"]
        assert task["status"] in ("QUEUED", "RUNNING", "WAITING_APPROVAL",
                                  "PAUSED")
        assert task["version"] >= 1
        fetched = client.get(f"/api/v1/tasks/{task['task_id']}",
                             headers=headers)
        assert fetched.status_code == 200
        assert fetched.json()["task"]["requirement"] == \
            "Add CSV export functionality"
        listed = client.get("/api/v1/tasks", headers=headers)
        assert listed.json()["total"] >= 1


def test_task_submit_validation(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        empty = client.post("/api/v1/tasks", headers=headers,
                            json={"requirement": ""})
        assert empty.status_code == 400
        assert empty.json()["error"]["code"] == "INVALID_REQUEST"
        bad_mode = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "x", "mode": "godmode"})
        assert bad_mode.status_code == 400


def test_unknown_task_is_404_with_request_id(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        response = client.get("/api/v1/tasks/t-0000000000000000",
                              headers=headers)
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "TASK_NOT_FOUND"
        assert error["request_id"] == response.headers["X-Request-ID"]


def test_pause_resume_queued_task(tmp_path):
    # No worker yet (and no lifespan runner): the task stays queued, so the
    # queued pause/resume path is deterministic.
    plane, client = _setup(tmp_path, start=False)
    try:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]
        current = task_state(client, headers, task_id)
        assert current["status"] == "QUEUED"
        paused = client.post(f"/api/v1/tasks/{task_id}/pause",
                             headers=headers, json={
                                 "expected_version": current["version"]})
        assert paused.status_code == 200
        assert paused.json()["task"]["status"] == "PAUSED"
        resumed = client.post(
            f"/api/v1/tasks/{task_id}/resume", headers=headers,
            json={"expected_version":
                  paused.json()["task"]["version"]})
        assert resumed.status_code == 200
        assert resumed.json()["task"]["status"] == "QUEUED"
    finally:
        plane.close()


def test_stale_version_conflicts(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]
        response = client.post(f"/api/v1/tasks/{task_id}/pause",
                               headers=headers,
                               json={"expected_version": 999999})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "TASK_CONFLICT"


def test_terminal_task_rejects_pause_cancel(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]
        final = drive_to_terminal(client, headers, task_id)
        assert final["status"] in ("SUCCEEDED", "FAILED")
        paused = client.post(f"/api/v1/tasks/{task_id}/pause",
                             headers=headers, json={})
        assert paused.status_code == 409
        assert paused.json()["error"]["code"] == "TASK_NOT_RUNNING"
        cancelled = client.post(f"/api/v1/tasks/{task_id}/cancel",
                                headers=headers, json={})
        assert cancelled.status_code == 409


def test_retry_finished_task_creates_new_attempt(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]
        final = drive_to_terminal(client, headers, task_id)
        retried = client.post(f"/api/v1/tasks/{task_id}/retry",
                              headers=headers)
        assert retried.status_code == 200
        payload = retried.json()["task"]
        assert payload["task_id"] != task_id
        assert payload["retry_of"] == task_id
        assert payload["attempts"] == final["attempts"] + 1


def test_retry_running_task_conflicts(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]
        # Retry immediately: either still active (conflict) or already
        # terminal (accepted). Both are honest; assert accordingly.
        current = task_state(client, headers, task_id)
        response = client.post(f"/api/v1/tasks/{task_id}/retry",
                               headers=headers)
        if current["status"] in ("SUCCEEDED", "FAILED", "CANCELLED",
                                 "ROLLED_BACK"):
            assert response.status_code == 200
        else:
            assert response.status_code == 409


def test_runs_aliases(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]
        runs = client.get("/api/v1/runs", headers=headers)
        assert runs.status_code == 200
        assert runs.json()["total"] >= 1
        run = client.get(f"/api/v1/runs/{task_id}", headers=headers)
        assert run.status_code == 200
        assert run.json()["run"]["task_id"] == task_id
        assert "report" in run.json()["run"]


def test_report_logs_verification_views(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]
        drive_to_terminal(client, headers, task_id)
        report = client.get(f"/api/v1/tasks/{task_id}/report",
                            headers=headers)
        assert report.status_code == 200
        assert "report" in report.json()
        logs = client.get(f"/api/v1/tasks/{task_id}/logs",
                          headers=headers)
        assert logs.status_code == 200
        assert "stages" in logs.json()
        verification = client.get(
            f"/api/v1/tasks/{task_id}/verification", headers=headers)
        assert verification.status_code == 200
        assert "acceptance" in verification.json()


def test_models_and_providers_views(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        models = client.get("/api/v1/models", headers=headers)
        assert models.status_code == 200
        assert models.json()["models"]
        assert "routing_policy" in models.json()
        health = client.get("/api/v1/models/health", headers=headers)
        assert health.status_code == 200
        assert "models" in health.json()
        providers = client.get("/api/v1/providers", headers=headers)
        assert providers.status_code == 200
        assert providers.json()["providers"]


def test_permissions_view_reflects_profile(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client, profile="assisted")
        permissions = client.get("/api/v1/permissions", headers=headers)
        assert permissions.status_code == 200
        payload = permissions.json()
        assert payload["profile"] == "assisted"
        assert payload["effective"]["write_file"] == "approval_required"
        assert payload["effective"]["read_file"] == "safe"
        _, _, safe_headers = login(client, actor="bob", profile="safe")
        safe = client.get("/api/v1/permissions", headers=safe_headers)
        assert safe.json()["profile"] == "safe"


def test_git_read_views(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        status = client.get("/api/v1/projects/demo/git", headers=headers)
        assert status.status_code == 200
        assert status.json()["available"] is True
        assert status.json()["branch"]
        diff = client.get("/api/v1/projects/demo/git/diff",
                          headers=headers)
        assert diff.status_code == 200
        assert "diff" in diff.json()


def test_dashboard_counts_are_real(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        before = client.get("/api/v1/dashboard",
                            headers=headers).json()["tasks"]
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]
        drive_to_terminal(client, headers, task_id)
        after = client.get("/api/v1/dashboard",
                           headers=headers).json()["tasks"]
        assert (after["succeeded"] + after["failed"]) == (
            before["succeeded"] + before["failed"] + 1)


def test_checkpoints_listed_for_task(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]
        wait_for_status(client, headers, task_id,
                        ("RUNNING", "WAITING_APPROVAL", "PAUSED",
                         "SUCCEEDED", "FAILED", "CANCELLED"),
                        timeout=60.0)
        checkpoints = client.get(
            f"/api/v1/tasks/{task_id}/checkpoints", headers=headers)
        assert checkpoints.status_code == 200
        assert checkpoints.json()["checkpoints"]
        assert checkpoints.json()["checkpoints"][0]["checkpoint_id"]
