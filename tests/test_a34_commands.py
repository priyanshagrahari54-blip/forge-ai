"""Safe command vocabulary, NL, voice, and desktop tests (A34)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import (  # noqa: E402
    login,
    make_client,
    make_plane,
    make_repo,
    task_state,
)


def _setup(tmp_path, **kwargs):
    plane = make_plane(tmp_path, **kwargs)
    make_repo(Path(plane.projects["demo"].root))
    return plane, make_client(plane)


def test_commands_execute_finite_vocabulary(tmp_path):
    plane, client = _setup(tmp_path, start=False)
    try:
        _, _, headers = login(client)
        started = client.post("/api/v1/commands", headers=headers, json={
            "command": "START_TASK",
            "requirement": "Add CSV export functionality"})
        assert started.status_code == 200
        task_id = started.json()["task"]["task_id"]
        assert task_state(client, headers, task_id)["status"] == "QUEUED"
        paused = client.post("/api/v1/commands", headers=headers, json={
            "command": "PAUSE_TASK", "task_id": task_id})
        assert paused.status_code == 200
        assert paused.json()["task"]["status"] == "PAUSED"
        resumed = client.post("/api/v1/commands", headers=headers, json={
            "command": "RESUME_TASK", "task_id": task_id})
        assert resumed.status_code == 200
        cancelled = client.post("/api/v1/commands", headers=headers, json={
            "command": "CANCEL_TASK", "task_id": task_id})
        assert cancelled.status_code == 200
        assert cancelled.json()["task"]["status"] == "CANCELLED"
    finally:
        plane.close()


def test_unknown_commands_rejected(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        for bad in ("RUN_SHELL", "rm -rf", "EXECUTE", "", "start_task"):
            response = client.post("/api/v1/commands", headers=headers,
                                   json={"command": bad})
            assert response.status_code == 400, bad
            assert response.json()["error"]["code"] == "INVALID_REQUEST"
        missing = client.post("/api/v1/commands", headers=headers, json={
            "command": "PAUSE_TASK"})
        assert missing.status_code == 400


def test_interpret_never_executes(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        shell = client.post("/api/v1/interpret", headers=headers, json={
            "text": "run rm -rf / on the server"}).json()
        assert shell["interpretation"]["kind"] == "DENY"
        assert shell["executed"] is False
        unknown = client.post("/api/v1/interpret", headers=headers, json={
            "text": "please ponder the meaning of tests"}).json()
        assert unknown["interpretation"]["kind"] == \
            "REQUIRE_CLARIFICATION"
        assert unknown["executed"] is False
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"}).json()
        task_id = created["task"]["task_id"]
        pause = client.post("/api/v1/interpret", headers=headers, json={
            "text": f"pause task {task_id}"}).json()
        assert pause["interpretation"]["kind"] == "COMMAND"
        assert pause["interpretation"]["command"] == "PAUSE_TASK"
        assert pause["interpretation"]["slots"]["task_id"] == task_id
        assert pause["executed"] is False
        # Nothing executed: the task is untouched by interpretation.
        assert task_state(client, headers, task_id)["status"] in (
            "QUEUED", "RUNNING", "WAITING_APPROVAL", "PAUSED")


def test_voice_foundation_is_advisory(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        known = client.post("/api/v1/voice/interpret", headers=headers,
                            json={"text": "check status"}).json()
        assert known["intent"]["name"] == "status"
        assert "permission" in known
        assert known["executed"] is False
        unknown = client.post("/api/v1/voice/interpret", headers=headers,
                              json={"text": "launch the missiles"}).json()
        assert unknown["intent"]["name"] == "unknown"
        assert unknown["permission"]["allowed"] is False
        assert unknown["executed"] is False


def test_desktop_a35_simulation(tmp_path):
    """A35 upgraded the desktop foundation into a controlled execution
    architecture over the fake desktop provider (honestly labeled)."""
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        capabilities = client.get("/api/v1/desktop/capabilities",
                                  headers=headers).json()
        assert capabilities["status"] == "simulation"
        assert capabilities["capabilities"]
        assert any(item["executable"] is True
                   for item in capabilities["capabilities"])
        assert "fake" in capabilities["provider"].lower()
        checked = client.post("/api/v1/desktop/check", headers=headers,
                              json={"action": "mouse_click",
                                    "target": "screen"}).json()
        assert checked["action"] == "mouse_click"
        assert checked["executed"] is False
        assert checked["decision"] in ("ALLOW", "DENY",
                                       "REQUIRE_APPROVAL")
        invalid = client.post("/api/v1/desktop/check", headers=headers,
                              json={"action": "format_disk"})
        assert invalid.status_code == 400
