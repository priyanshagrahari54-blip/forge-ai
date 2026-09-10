"""Agent evolution (A50): honest evidence snapshots from real runs."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import (approve_all, login, make_client, make_plane,  # noqa: E402
                         make_repo, wait_for_status)  # noqa: E402
from forge.models.provider import ModelResult  # noqa: E402


def drive_to_terminal(client, headers, task_id):
    import time as _time
    from helpers_a34 import task_state
    deadline = _time.time() + 60.0
    while _time.time() < deadline:
        approve_all(client, headers)
        current = task_state(client, headers, task_id)
        if current["status"] in ("SUCCEEDED", "FAILED", "CANCELLED",
                                 "ROLLED_BACK"):
            return current
        _time.sleep(0.2)
    raise AssertionError(f"run never finished: {current}")


class SlowProvider:
    """Completes a real run after a short delay (no blocking waits)."""

    name = "slow"

    def generate(self, prompt, *, context="", task="",
                 instructions="", max_output_tokens=None, temperature=None):
        import time
        time.sleep(2.0)
        return ModelResult('{"summary": "slow", "changes": []}', self.name)

from forge.agents.evolution import AgentEvolution  # noqa: E402


def test_definitions_start_unrecorded():
    plane_root = None  # factory test only
    del plane_root
    from forge.agents.factory import AgentFactory
    factory = AgentFactory("s1")
    definition = factory.create("worker", "coding", ("coding",))
    assert definition.generation == 1
    assert definition.metrics == {}
    assert "generation" in definition.to_dict()
    assert definition.to_dict()["metrics"] == {}


def test_evolution_records_real_terminal_outcome(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "worker", "coding", ["coding"])
        task = plane.submit_task(session, "add CSV export",
                                 mode="assisted")
        task_id = task.id
        drive_to_terminal(client, headers, task_id)
        result = plane.agent_record_outcome(session, "worker", task_id)
        assert result["generation"] == 2
        assert result["metrics"]["runs"] == 1
        assert result["metrics"]["succeeded"] + \
            result["metrics"]["failed"] == 1
        evolution = plane.agent_evolution(session, "worker")
        assert evolution["generation"] == 2
        assert evolution["metrics"]["success_rate"] in (0.0, 1.0)


def test_evolution_refuses_unfinished_runs(tmp_path):
    plane = make_plane(tmp_path, start=True, provider=SlowProvider())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "worker", "coding", ["coding"])
        task = plane.submit_task(session, "add CSV export",
                                 mode="assisted")
        task_id = task.id
        wait_for_status(client, headers, task_id, "RUNNING")
        with pytest.raises(Exception):
            plane.agent_record_outcome(session, "worker", task_id)
        evolution = plane.agent_evolution(session, "worker")
        assert evolution["metrics"] == {}
        wait_for_status(client, headers, task_id,
                        {"SUCCEEDED", "FAILED", "CANCELLED"})


def test_evolution_unknown_targets_refused(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        with pytest.raises(Exception):
            plane.agent_record_outcome(session, "ghost", "nope")
        with pytest.raises(Exception):
            plane.agent_evolution(session, "ghost")


def test_evolution_api(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        created = client.post("/api/v1/agents", headers=headers,
                              json={"name": "worker", "role": "coding",
                                    "capabilities": ["coding"]})
        assert created.status_code == 200
        task = client.post("/api/v1/tasks", headers=headers,
                           json={"requirement": "add CSV export",
                                 "mode": "assisted"})
        task_id = task.json()["task"]["task_id"]
        drive_to_terminal(client, headers, task_id)
        recorded = client.post(
            "/api/v1/agents/worker/outcomes", headers=headers,
            json={"task_id": task_id})
        assert recorded.status_code == 200
        assert recorded.json()["generation"] == 2
        evolution = client.get("/api/v1/agents/worker/evolution",
                               headers=headers)
        assert evolution.status_code == 200
        assert evolution.json()["metrics"]["runs"] == 1
        missing = client.get("/api/v1/agents/ghost/evolution",
                             headers=headers)
        assert missing.status_code == 400


def test_metrics_are_computed_not_invented():
    evolution = AgentEvolution("s1")

    class FakeDef:
        name = "w"
        generation = 1
        metrics: dict = {}

    class FakeRun:
        status = "SUCCEEDED"
        attempts = 2
        started_at = 100.0
        finished_at = 101.5
        created_at = 100.0
        updated_at = 101.5

    definition = FakeDef()
    evolution.record(definition, FakeRun())
    snapshot = evolution.snapshot("w")
    assert snapshot["runs"] == 1
    assert snapshot["succeeded"] == 1
    assert snapshot["failed"] == 0
    assert snapshot["success_rate"] == 1.0
    assert snapshot["avg_attempts"] == 2.0
    assert snapshot["avg_elapsed_ms"] == 1500.0
    assert evolution.snapshot("nobody") == {}
