"""Observability (A62): honest metrics from real pipeline events."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)
from helpers_a34 import (drive_to_terminal, login, make_client,  # noqa: E402
                         make_plane, make_repo)


def run_policy() -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="a-run", resource=Resource.AGENT,
                       operation="execute", scope="", effect="ALLOW"),
        PermissionRule(id="fs-w", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect="ALLOW"),
    ])


def test_task_events_produce_real_counters(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        snapshot = plane.observability_snapshot(session)
        assert snapshot["counters"].get("tasks.submitted", 0) == 0
        task = plane.submit_task(session, "add a csv export")
        drive_to_terminal(client, headers, task.id)
        snapshot = plane.observability_snapshot(session)
        counters = snapshot["counters"]
        assert counters["tasks.submitted"] == 1
        assert counters["runs.succeeded"] == 1
        assert "runs.failed" not in counters
        duration = snapshot["latencies"]["run.duration_ms"]
        assert duration["count"] == 1
        assert duration["mean_ms"] >= 0
        assert duration["p95_ms"] >= duration["p50_ms"] >= 0


def test_failed_runs_are_counted_honestly(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "spec-only", "coding", ["coding"])
        run = plane.agent_run(session, "spec-only", "write something")
        deadline = time.time() + 15.0
        while deadline > time.time():
            state = plane.agent_run_result(session, "spec-only",
                                           run["run_id"])
            if state["status"] in ("failed", "finished"):
                break
            time.sleep(0.05)
        snapshot = plane.observability_snapshot(session)
        assert snapshot["counters"]["agent_runs.failed"] == 1
        assert snapshot["counters"].get("tasks.submitted", 0) == 0


def test_gauges_reflect_plane_state(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "scout", "research", ["research"],
                           bind=True)
        plane.team_create(session, "solo", ["scout"])
        snapshot = plane.observability_snapshot(session)
        gauges = snapshot["gauges"]
        assert gauges["active_sessions"] >= 1
        assert gauges["agents_defined"] == 1
        assert gauges["teams_defined"] == 1
        assert gauges["active_runs"] >= 0


def test_snapshot_is_stable_and_consistent(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        first = plane.observability_snapshot(session)
        second = plane.observability_snapshot(session)
        assert first == second
        assert set(first) == {"counters", "latencies", "gauges"}


def test_observability_api(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        response = client.get("/api/v1/observability/metrics",
                              headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"counters", "latencies", "gauges"}
        assert body["gauges"]["active_sessions"] >= 1
