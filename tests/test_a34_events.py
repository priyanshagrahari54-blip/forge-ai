"""Event history, replay, and live stream tests (A34)."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import (  # noqa: E402
    drive_to_terminal,
    login,
    make_client,
    make_plane,
    make_repo,
    run_server,
)


def _setup(tmp_path, **kwargs):
    plane = make_plane(tmp_path, **kwargs)
    make_repo(Path(plane.projects["demo"].root))
    return plane, make_client(plane)


def _read_sse(response, want_events: int, timeout: float = 30.0):
    """Read SSE frames until want_events data frames arrive."""
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


def test_historical_events_and_cursor_replay(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]
        drive_to_terminal(client, headers, task_id)
        full = client.get(f"/api/v1/tasks/{task_id}/events",
                          headers=headers).json()
        assert full["events"]
        seqs = [event["seq"] for event in full["events"]]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)
        midpoint = seqs[len(seqs) // 2]
        replay = client.get(
            f"/api/v1/tasks/{task_id}/events?after={midpoint}",
            headers=headers).json()
        assert replay["events"]
        assert all(event["seq"] > midpoint
                   for event in replay["events"])
        # History + replay reconcile exactly: no gaps, no duplicates.
        before = [event for event in full["events"]
                  if event["seq"] <= midpoint]
        assert [event["seq"] for event in before] + \
            [event["seq"] for event in replay["events"]] == seqs
        assert replay["latest"] == full["latest"]


def test_event_limit_is_bounded(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        task_id = created.json()["task"]["task_id"]
        drive_to_terminal(client, headers, task_id)
        one = client.get(f"/api/v1/tasks/{task_id}/events?limit=1",
                         headers=headers).json()
        assert len(one["events"]) == 1
        many = client.get(f"/api/v1/tasks/{task_id}/events?limit=99999",
                          headers=headers).json()
        assert len(many["events"]) <= 500


def test_live_stream_delivers_and_replays(tmp_path):
    import httpx

    plane, _ = _setup(tmp_path)
    try:
        # Live bytes over a real socket: TestClient buffers whole responses.
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
                received: list[dict] = []
                with http.stream(
                        "GET",
                        f"/api/v1/tasks/{task_id}/events/stream?after=0",
                        headers=headers) as response:
                    assert response.status_code == 200
                    assert "text/event-stream" in response.headers[
                        "content-type"]
                    received.extend(_read_sse(response, 6, timeout=25.0))
            assert len(received) >= 4
            types = [event["type"] for event in received]
            assert "task.created" in types
            assert all(event["task_id"] == task_id for event in received)
            assert "event_id" in received[0]
            assert "timestamp" in received[0]
            seqs = [event["seq"] for event in received]
            assert len(set(seqs)) == len(seqs)
    finally:
        plane.close()


def test_stream_requires_auth(tmp_path):
    import httpx

    plane, _ = _setup(tmp_path)
    try:
        with run_server(plane) as base:
            with httpx.Client(base_url=base, timeout=15.0) as http:
                session = http.post("/api/v1/sessions", json={
                    "actor": "alice", "project_id": "demo"}).json()
                headers = {"Authorization":
                           f"Bearer {session['token']}"}
                created = http.post(
                    "/api/v1/tasks", headers=headers,
                    json={"requirement": "Add CSV export functionality"})
                task_id = created.json()["task"]["task_id"]
            # Fresh client: no bearer token, no session cookie.
            with httpx.Client(base_url=base, timeout=15.0) as anon:
                with anon.stream(
                        "GET",
                        f"/api/v1/tasks/{task_id}/events/stream") as response:
                    assert response.status_code == 401
    finally:
        plane.close()


def test_stream_unknown_task_is_404(tmp_path):
    import httpx

    plane, _ = _setup(tmp_path)
    try:
        with run_server(plane) as base:
            with httpx.Client(base_url=base, timeout=15.0) as http:
                session = http.post("/api/v1/sessions", json={
                    "actor": "alice", "project_id": "demo"}).json()
                headers = {"Authorization":
                           f"Bearer {session['token']}"}
                with http.stream(
                        "GET",
                        "/api/v1/tasks/t-0000000000000000/events/stream",
                        headers=headers) as response:
                    assert response.status_code == 404
    finally:
        plane.close()


def test_events_isolated_between_tasks(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        first = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "First task"}).json()["task"]["task_id"]
        second = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Second task"}).json()["task"]["task_id"]
        first_events = client.get(f"/api/v1/tasks/{first}/events",
                                  headers=headers).json()["events"]
        assert first_events
        assert all(event["task_id"] == first for event in first_events)
        second_events = client.get(f"/api/v1/tasks/{second}/events",
                                   headers=headers).json()["events"]
        assert all(event["task_id"] == second for event in second_events)
        # A cursor from task A yields nothing under task B.
        latest = max(event["seq"] for event in first_events)
        cross = client.get(
            f"/api/v1/tasks/{second}/events?after={latest}",
            headers=headers).json()["events"]
        assert all(event["task_id"] == second for event in cross)
