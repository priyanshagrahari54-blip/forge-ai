"""Performance (A63): real run timings and bounded aggregates."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.control.control_plane import Run, RunStatus  # noqa: E402
from helpers_a34 import (ScriptedProvider, drive_to_terminal,  # noqa: E402
                         login, make_client, make_plane, make_repo)


class SlowProvider(ScriptedProvider):
    """Deterministic provider that takes a measurable amount of time."""

    name = "slow"

    def generate(self, prompt, **kwargs):
        time.sleep(0.3)
        return super().generate(prompt, **kwargs)


def test_run_profile_from_real_timestamps(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        task = plane.submit_task(session, "add a csv export")
        drive_to_terminal(client, headers, task.id)
        profile = plane.performance_run(session, task.id)
        assert profile["run_id"] == task.id
        assert profile["finished"] is True
        assert profile["queue_ms"] is not None
        assert profile["queue_ms"] >= 0
        assert profile["execution_ms"] >= 0
        assert profile["total_ms"] >= profile["execution_ms"]
        # The slow provider dominates execution time.
        assert profile["execution_ms"] >= 250


def test_performance_summary_aggregates_runs(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        first = plane.submit_task(session, "add a csv export")
        drive_to_terminal(client, headers, first.id)
        second = plane.submit_task(session, "tidy the helpers")
        drive_to_terminal(client, headers, second.id)
        summary = plane.performance_summary(session)
        assert summary["runs_considered"] == 2
        assert summary["terminal_runs"] == 2
        assert summary["execution_ms"]["count"] == 2
        assert summary["execution_ms"]["mean_ms"] >= 0
        assert summary["execution_ms"]["max_ms"] >= \
            summary["execution_ms"]["mean_ms"]
        assert summary["runs_by_mode"] == {"assisted": 2}
        assert len(summary["slowest_runs"]) == 2
        assert summary["slowest_runs"][0]["execution_ms"] >= \
            summary["slowest_runs"][1]["execution_ms"]


def test_performance_validation_and_isolation(tmp_path):
    plane = make_plane(tmp_path, start=True,
                       extra_projects={"other": str(tmp_path / "other")})
    make_repo(Path(plane.projects["demo"].root))
    make_repo(Path(plane.projects["other"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        with pytest.raises(Exception):
            plane.performance_run(session, "t-0000000000000000")
        with pytest.raises(Exception):
            plane.performance_summary(session, limit=0)
        with pytest.raises(Exception):
            plane.performance_summary(session, limit=201)
        # A run from another project must not leak through.
        foreign = plane.runs.create(Run(
            id="t-ff00000000000001", project_id="other",
            requirement="foreign", status=RunStatus.QUEUED,
            stage="queued", version=1, mode="assisted", actor="bob",
            created_at=time.time(), updated_at=time.time()))
        with pytest.raises(Exception):
            plane.performance_run(session, foreign.id)


def test_pending_run_profile_is_honest(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        task = plane.submit_task(session, "add a csv export")
        profile = plane.performance_run(session, task.id)
        assert profile["finished"] is False
        assert profile["execution_ms"] is None
        drive_to_terminal(client, headers, task.id)


def test_performance_api(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        summary = client.get("/api/v1/performance/summary",
                             headers=headers)
        assert summary.status_code == 200
        body = summary.json()
        assert body["runs_considered"] == 0
        missing = client.get("/api/v1/performance/runs/t-unknown-0000",
                             headers=headers)
        assert missing.status_code == 404
        task = plane.submit_task(
            plane.sessions.get(_session["session_id"]),
            "add a csv export")
        drive_to_terminal(client, headers, task.id)
        profile = client.get(f"/api/v1/performance/runs/{task.id}",
                             headers=headers)
        assert profile.status_code == 200
        assert profile.json()["finished"] is True
        assert profile.json()["execution_ms"] >= 0
