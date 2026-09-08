"""Agent teams (A52): validated ordered teams, sequential real runs."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402

from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)


def run_policy(effect: str = "ALLOW") -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="a-run", resource=Resource.AGENT,
                       operation="execute", scope="", effect=effect),
        PermissionRule(id="fs-w", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect="ALLOW"),
    ])


def wait_team(plane, session, team_id, run_id, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = plane.team_run_result(session, team_id, run_id)
        if state["status"] == "finished":
            return state
        if state["status"] == "failed":
            raise AssertionError(state.get("error"))
        time.sleep(0.1)
    raise AssertionError("team run never finished")


def make_pair(plane, session):
    plane.agent_create(session, "scout", "research", ["research"],
                       bind=True)
    plane.agent_create(session, "strategist", "planning", ["planning"],
                       bind=True)


def test_team_creation_validates_members(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        make_pair(plane, session)
        plane.agent_create(session, "spec-only", "coding", ["coding"])
        with pytest.raises(Exception):
            plane.team_create(session, "t1", ["ghost"])
        with pytest.raises(Exception):
            plane.team_create(session, "t1", ["spec-only"])
        with pytest.raises(Exception):
            plane.team_create(session, "t1", [])
        with pytest.raises(Exception):
            plane.team_create(session, "t1",
                              ["scout", "strategist", "scout",
                               "strategist", "scout", "strategist",
                               "scout"])
        team = plane.team_create(session, "duo",
                                 ["scout", "strategist"])
        assert team["members"] == ["scout", "strategist"]


def test_team_runs_members_sequentially_with_real_results(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        make_pair(plane, session)
        team = plane.team_create(session, "duo",
                                 ["scout", "strategist"])
        dispatched = plane.team_execute(
            session, team["team_id"],
            "debug the failing export and count the python files")
        assert dispatched["allowed"] is True
        state = wait_team(plane, session, team["team_id"],
                          dispatched["run_id"])
        assert state["success"] is True
        assert [item["agent"] for item in state["results"]] == \
            ["scout", "strategist"]
        assert '"python_files": 2' in state["results"][0]["output"]
        assert "debugging" in state["results"][1]["output"]
        # The handoff context is real: the planner saw the summary.
        record = plane.runs.get(dispatched["task_id"])
        assert record.status.value in ("SUCCEEDED",)


def test_team_denied_fails_closed(tmp_path):
    plane = make_plane(tmp_path, start=True,
                       policy=run_policy(effect="DENY"))
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        make_pair(plane, session)
        team = plane.team_create(session, "duo",
                                 ["scout", "strategist"])
        dispatched = plane.team_execute(session, team["team_id"],
                                        "count the python files")
        assert dispatched["allowed"] is True  # dispatch ok, gate per run
        state = wait_team(plane, session, team["team_id"],
                          dispatched["run_id"])
        assert state["success"] is False
        assert state["results"][0]["success"] is False


def test_teams_are_session_isolated(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        alice = plane.sessions.get(payload["session_id"])
        make_pair(plane, alice)
        team = plane.team_create(alice, "duo", ["scout", "strategist"])
        _bs, _bt, _bh = login(client, actor="bob")
        bob = plane.sessions.get(_bs["session_id"])
        assert plane.team_list(bob)["teams"] == []
        with pytest.raises(Exception):
            plane.team_execute(bob, team["team_id"], "anything")


def test_team_api(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        for spec in (("scout", "research"), ("strategist", "planning")):
            created = client.post("/api/v1/agents", headers=headers,
                                  json={"name": spec[0],
                                        "role": spec[1],
                                        "capabilities": [spec[1]],
                                        "bind": True})
            assert created.status_code == 200
        team = client.post("/api/v1/teams", headers=headers,
                           json={"name": "duo",
                                 "members": ["scout", "strategist"]})
        assert team.status_code == 200
        team_id = team.json()["team_id"]
        assert client.post("/api/v1/teams", headers=headers,
                           json={"name": "bad",
                                 "members": ["ghost"]}
                           ).status_code == 400
        run = client.post(f"/api/v1/teams/{team_id}/execute",
                          headers=headers,
                          json={"requirement": "count the python files"})
        assert run.status_code == 200
        run_id = run.json()["run_id"]
        deadline = time.time() + 30.0
        state = None
        while time.time() < deadline:
            state = client.get(
                f"/api/v1/teams/{team_id}/result/{run_id}",
                headers=headers).json()
            if state["status"] == "finished":
                break
            time.sleep(0.1)
        assert state is not None
        assert state["success"] is True
        listed = client.get("/api/v1/teams", headers=headers)
        assert listed.status_code == 200
        assert [item["name"] for item in listed.json()["teams"]] == ["duo"]
