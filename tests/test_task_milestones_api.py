"""Task milestone projection API tests."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import drive_to_terminal, login, make_client, make_plane  # noqa: E402


EXPECTED_STAGES = {
    "planning", "coding", "testing", "debugging", "review",
    "security", "benchmark", "acceptance", "checkpoint", "commit",
    "completed",
}


def _setup(tmp_path):
    plane = make_plane(tmp_path)
    return plane, make_client(plane)


def test_task_milestones_is_authenticated_and_event_backed(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        unauth = client.get("/api/v1/tasks/missing/milestones")
        assert unauth.status_code == 401

        _, _, headers = login(client)
        created = client.post(
            "/api/v1/tasks",
            headers=headers,
            json={"requirement": "Build a small verified feature"},
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["task"]["task_id"]

        drive_to_terminal(client, headers, task_id)
        response = client.get(
            f"/api/v1/tasks/{task_id}/milestones", headers=headers
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["task_id"] == task_id
        assert {item["id"] for item in payload["milestones"]} == EXPECTED_STAGES
        assert payload["milestones"]
        assert payload["milestones"][-1]["id"] == "completed"
        assert payload["source"] == "durable-event-projection"
        assert payload["observed_events"] >= 1


def test_task_milestones_rejects_unknown_task(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        response = client.get(
            "/api/v1/tasks/not-a-real-task/milestones", headers=headers
        )
        assert response.status_code == 404
