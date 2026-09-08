"""Agent lifecycle (A55): validated status transitions gate execution."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402

from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)

RUN_POLICY = PermissionPolicy(rules=[
    PermissionRule(id="a-run", resource=Resource.AGENT,
                   operation="execute", scope="", effect="ALLOW"),
    PermissionRule(id="fs-w", resource=Resource.FILESYSTEM,
                   operation="write", scope="**", effect="ALLOW"),
])


def test_new_agents_are_active(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=RUN_POLICY)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        created = plane.agent_create(session, "scout", "research",
                                     ["research"], bind=True)
        assert created["status"] == "active"


def test_valid_transitions_and_refusals(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=RUN_POLICY)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "scout", "research", ["research"],
                           bind=True)
        paused = plane.agent_set_status(session, "scout", "paused")
        assert paused["status"] == "paused"
        active = plane.agent_set_status(session, "scout", "active")
        assert active["status"] == "active"
        retired = plane.agent_set_status(session, "scout", "retired")
        assert retired["status"] == "retired"
        with pytest.raises(Exception):
            plane.agent_set_status(session, "scout", "active")
        with pytest.raises(Exception):
            plane.agent_set_status(session, "scout", "paused")
        # Direct active -> retired is fine; active -> active is not.
        plane.agent_create(session, "worker", "research", ["research"],
                           bind=True)
        with pytest.raises(Exception):
            plane.agent_set_status(session, "worker", "active")
        with pytest.raises(Exception):
            plane.agent_set_status(session, "worker", "bogus")


def test_paused_agents_refuse_to_run(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=RUN_POLICY)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "scout", "research", ["research"],
                           bind=True)
        plane.agent_set_status(session, "scout", "paused")
        with pytest.raises(Exception):
            plane.agent_run(session, "scout", "count files")
        assert plane.agent_runs(session, "scout")["runs"] == []


def test_teams_require_active_members(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=RUN_POLICY)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "scout", "research", ["research"],
                           bind=True)
        plane.agent_set_status(session, "scout", "paused")
        with pytest.raises(Exception):
            plane.team_create(session, "solo", ["scout"])


def test_lifecycle_api(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=RUN_POLICY)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        client.post("/api/v1/agents", headers=headers,
                    json={"name": "scout", "role": "research",
                          "capabilities": ["research"], "bind": True})
        paused = client.post("/api/v1/agents/scout/status", headers=headers,
                             json={"status": "paused"})
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"
        run = client.post("/api/v1/agents/scout/run", headers=headers,
                          json={"requirement": "count files"})
        assert run.status_code == 400
        retired = client.post("/api/v1/agents/scout/status",
                              headers=headers,
                              json={"status": "retired"})
        assert retired.status_code == 200
        resurrect = client.post("/api/v1/agents/scout/status",
                                headers=headers,
                                json={"status": "active"})
        assert resurrect.status_code == 400
