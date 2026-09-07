"""Disconnected-browser tests (A34).

The backend is authoritative: closing the stream (or the tab) never stops
a run, and reconnecting with a cursor replays exactly the missed events.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import (  # noqa: E402
    make_plane,
    make_repo,
    run_server,
)


def _read_sse(response, want_events: int, timeout: float = 30.0):
    events = []
    deadline = time.time() + timeout
    for line in response.iter_lines():
        if time.time() > deadline:
            break
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            text = line[5:].strip()
            if text == '{"reason": "stream-closed"}':
                break
            try:
                events.append(json.loads(text))
            except ValueError:
                pass
            if len(events) >= want_events:
                break
    return events


def test_disconnect_reconnect_with_replay(tmp_path):
    import httpx

    plane = make_plane(tmp_path)
    make_repo(Path(plane.projects["demo"].root))
    terminal = {"SUCCEEDED", "FAILED", "CANCELLED", "ROLLED_BACK"}
    try:
        with run_server(plane) as base:
            with httpx.Client(base_url=base, timeout=30.0) as http:
                session = http.post("/api/v1/sessions", json={
                    "actor": "alice", "project_id": "demo"}).json()
                headers = {"Authorization":
                           f"Bearer {session['token']}"}
                created = http.post(
                    "/api/v1/tasks", headers=headers,
                    json={"requirement": "Add CSV export functionality"})
                assert created.status_code == 200
                task_id = created.json()["task"]["task_id"]

                # Connect, read a few events, then disconnect hard.
                first_batch: list[dict] = []
                with http.stream(
                        "GET",
                        f"/api/v1/tasks/{task_id}/events/stream?after=0",
                        headers=headers) as response:
                    assert response.status_code == 200
                    first_batch.extend(_read_sse(response, 3,
                                                 timeout=20.0))
                assert len(first_batch) >= 3
                cursor = max(event["seq"] for event in first_batch)

                # Drive the run to completion with NO stream attached.
                deadline = time.time() + 180.0
                final: dict = {}
                while time.time() < deadline:
                    approvals = http.get("/api/v1/approvals",
                                         headers=headers).json()["approvals"]
                    for approval in approvals:
                        decided = http.post(
                            f"/api/v1/approvals/{approval['id']}/approve",
                            headers=headers)
                        assert decided.status_code == 200
                    final = http.get(f"/api/v1/tasks/{task_id}",
                                     headers=headers).json()["task"]
                    if final["status"] in terminal:
                        break
                    time.sleep(0.3)
                assert final["status"] in terminal, final
                assert final["status"] == "SUCCEEDED"

                # Reconnect with the cursor: only missed events arrive.
                second_batch: list[dict] = []
                with http.stream(
                        "GET",
                        f"/api/v1/tasks/{task_id}/events/stream"
                        f"?after={cursor}",
                        headers=headers) as response:
                    assert response.status_code == 200
                    second_batch.extend(_read_sse(response, 1000,
                                                  timeout=8.0))
                assert second_batch
                assert all(event["seq"] > cursor
                           for event in second_batch)

                # Historical + live reconcile exactly.
                history = http.get(
                    f"/api/v1/tasks/{task_id}/events?limit=500",
                    headers=headers).json()["events"]
                union = [event["seq"] for event in first_batch] + \
                    [event["seq"] for event in second_batch]
                assert sorted(union) == sorted(
                    event["seq"] for event in history)
                assert len(set(union)) == len(union)
    finally:
        plane.close()
