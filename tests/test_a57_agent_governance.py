"""Agent governance (A57): enforced per-agent runtime quotas."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import (BlockingProvider, approve_all, login,  # noqa: E402
                         make_client, make_plane, make_repo)

from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)


def run_policy() -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="a-run", resource=Resource.AGENT,
                       operation="execute", scope="", effect="ALLOW"),
        PermissionRule(id="fs-w", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect="ALLOW"),
    ])


def wait_terminal(plane, session, name, run_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = plane.agent_run_result(session, name, run_id)
        if result["status"] in ("finished", "failed"):
            return result
        time.sleep(0.1)
    raise AssertionError("agent run never finished")


def test_default_limits_and_validation(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "scout", "research", ["research"],
                           bind=True)
        limits = plane.agent_limits(session, "scout")
        assert limits["max_runs_per_hour"] == 60
        assert limits["max_concurrent"] == 2
        assert limits["runs_last_hour"] == 0
        assert limits["active_runs"] == 0
        tightened = plane.agent_set_limits(
            session, "scout", max_runs_per_hour=5, max_concurrent=1)
        assert tightened["max_runs_per_hour"] == 5
        assert tightened["max_concurrent"] == 1
        with pytest.raises(Exception):
            plane.agent_set_limits(session, "scout", max_runs_per_hour=0)
        with pytest.raises(Exception):
            plane.agent_set_limits(session, "scout", max_concurrent=21)
        with pytest.raises(Exception):
            plane.agent_set_limits(session, "ghost", max_runs_per_hour=5)
        with pytest.raises(Exception):
            plane.agent_limits(session, "ghost")


def test_hourly_run_limit_enforced_before_dispatch(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "scout", "research", ["research"],
                           bind=True)
        plane.agent_set_limits(session, "scout", max_runs_per_hour=1)
        first = plane.agent_run(session, "scout", "count python files")
        assert first["allowed"] is True
        terminal = wait_terminal(plane, session, "scout", first["run_id"])
        assert terminal["status"] == "finished"
        with pytest.raises(Exception) as exc_info:
            plane.agent_run(session, "scout", "again")
        assert "hourly" in str(exc_info.value).lower()
        limits = plane.agent_limits(session, "scout")
        assert limits["runs_last_hour"] == 1


def test_concurrency_limit_enforced_while_running(tmp_path):
    provider = BlockingProvider()
    plane = make_plane(tmp_path, start=True, provider=provider,
                       policy=run_policy(), approval_timeout=5.0)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "coder", "coding", ["coding"],
                           bind=True)
        plane.agent_set_limits(session, "coder", max_concurrent=1)
        first = plane.agent_run(session, "coder", "write the csv export")
        assert first["allowed"] is True
        assert provider.entered.wait(timeout=15.0), \
            "first run never reached the provider"
        limits = plane.agent_limits(session, "coder")
        assert limits["active_runs"] == 1
        with pytest.raises(Exception) as exc_info:
            plane.agent_run(session, "coder", "write more")
        assert "concurrency" in str(exc_info.value).lower()
        provider.release.set()
        deadline = time.time() + 30.0
        while True:
            result = plane.agent_run_result(session, "coder",
                                            first["run_id"])
            if result["status"] in ("finished", "failed"):
                break
            if result["status"] == "pending" and approve_all(
                    client, headers) > 0:
                pass
            assert time.time() < deadline, "first run never finished"
            time.sleep(0.1)
        limits = plane.agent_limits(session, "coder")
        assert limits["active_runs"] == 0
        # After the window frees up, runs are admitted again.
        second = plane.agent_run(session, "coder", "write again")
        assert second["allowed"] is True


def test_team_members_share_the_quota(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "scout", "research", ["research"],
                           bind=True)
        plane.agent_set_limits(session, "scout", max_runs_per_hour=1)
        first = plane.agent_run(session, "scout", "count python files")
        wait_terminal(plane, session, "scout", first["run_id"])
        # A team dispatching the same agent hits the same quota; the
        # team run fails honestly instead of bypassing the governor.
        team = plane.team_create(session, "solo", ["scout"])
        executed = plane.team_execute(session, team["team_id"],
                                      "count files")
        deadline = time.time() + 30.0
        result = None
        while time.time() < deadline:
            result = plane.team_run_result(session, team["team_id"],
                                           executed["run_id"])
            if result["status"] != "pending":
                break
            time.sleep(0.1)
        assert result["status"] == "failed"
        assert "hourly" in result.get("error", "").lower()


def test_governance_api(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        client.post("/api/v1/agents", headers=headers,
                    json={"name": "scout", "role": "research",
                          "capabilities": ["research"], "bind": True})
        put = client.put("/api/v1/agents/scout/limits", headers=headers,
                         json={"max_runs_per_hour": 1, "max_concurrent": 1})
        assert put.status_code == 200
        assert put.json()["max_runs_per_hour"] == 1
        bad = client.put("/api/v1/agents/scout/limits", headers=headers,
                         json={"max_runs_per_hour": 0,
                               "max_concurrent": 1})
        assert bad.status_code == 400
        run = client.post("/api/v1/agents/scout/run", headers=headers,
                          json={"requirement": "count python files"})
        assert run.status_code == 200
        deadline = time.time() + 30.0
        while time.time() < deadline:
            state = client.get(
                f"/api/v1/agents/scout/runs/{run.json()['run_id']}",
                headers=headers).json()
            if state.get("status") in ("finished", "failed"):
                break
            time.sleep(0.1)
        assert state["status"] == "finished"
        refused = client.post("/api/v1/agents/scout/run", headers=headers,
                              json={"requirement": "again"})
        assert refused.status_code == 400
        assert "hourly" in refused.json()["error"]["message"].lower()
        got = client.get("/api/v1/agents/scout/limits", headers=headers)
        assert got.status_code == 200
        assert got.json()["runs_last_hour"] == 1
