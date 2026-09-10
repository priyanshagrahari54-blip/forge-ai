"""Failure and recovery end-to-end tests (A34).

A breaking change must fail honestly: candidate files restored, unrelated
work preserved, rollback recorded, and the browser informed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import (  # noqa: E402
    FAILING_PAYLOAD,
    ScriptedProvider,
    drive_to_terminal,
    login,
    make_client,
    make_plane,
    make_repo,
)


def test_failing_change_rolls_back_exactly(tmp_path):
    plane = make_plane(tmp_path, ScriptedProvider(FAILING_PAYLOAD))
    root = Path(plane.projects["demo"].root)
    make_repo(root)
    (root / "unrelated.txt").write_text("keep me\n")
    client = make_client(plane)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]
        final = drive_to_terminal(client, headers, task_id, timeout=240.0)
        assert final["status"] == "FAILED"
        assert final["rollback"] is True
        assert final["error"]
        # Candidate changes removed, unrelated changes preserved.
        assert (root / "app.py").read_text() == \
            "def health(): return True\n"
        assert not (root / "tests" / "test_csv.py").exists()
        assert (root / "unrelated.txt").read_text() == "keep me\n"
        commits = subprocess.run(
            ["git", "log", "--oneline"], cwd=root, text=True,
            capture_output=True).stdout
        assert commits.count("initial") == 1
        # The browser received the failure story.
        events = client.get(f"/api/v1/tasks/{task_id}/events?limit=500",
                            headers=headers).json()["events"]
        types = [event["type"] for event in events]
        assert "tests.failed" in types
        assert "rollback.completed" in types
        assert "task.failed" in types
        report = client.get(f"/api/v1/tasks/{task_id}/report",
                            headers=headers).json()
        assert report["report"]["final_status"] == "FAILED"
        assert report["report"]["rollback"] is True
