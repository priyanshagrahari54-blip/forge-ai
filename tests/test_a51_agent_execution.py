"""Agent execution (A51): runtime-defined agents really run tasks."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

import time  # noqa: E402

from helpers_a34 import (approve_all, login, make_client, make_plane,  # noqa: E402
                         make_repo)

from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)


def wait_result(plane, session, name, run_id, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = plane.agent_run_result(session, name, run_id)
        if result["status"] == "finished":
            return result["run"]
        if result["status"] == "failed":
            raise AssertionError(result.get("error"))
        time.sleep(0.1)
    raise AssertionError("agent run never finished")


def run_policy(effect: str = "ALLOW", fs_effect: str = "ALLOW") \
        -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="a-run", resource=Resource.AGENT,
                       operation="execute", scope="", effect=effect),
        PermissionRule(id="fs-w", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect=fs_effect),
    ])


def test_unbound_agent_refuses_to_run(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "spec-only", "coding", ["coding"])
        result = plane.agent_run(session, "spec-only", "write something")
        assert result["allowed"] is True
        state = plane.agent_run_result(session, "spec-only",
                                       result["run_id"])
        deadline = time.time() + 10.0
        while state["status"] == "pending" and time.time() < deadline:
            time.sleep(0.05)
            state = plane.agent_run_result(session, "spec-only",
                                           result["run_id"])
        assert state["status"] == "failed"
        assert "without a bound executor" in state["error"]
        assert plane.agent_runs(session, "spec-only")["runs"] == []


def test_research_agent_reports_real_counts(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "scout", "research", ["research"],
                           bind=True)
        result = plane.agent_run(session, "scout", "count the python files")
        assert result["allowed"] is True
        run = wait_result(plane, session, "scout", result["run_id"])
        assert run["success"] is True
        assert '"python_files": 2' in run["output"]
        assert '"test_files": 1' in run["output"]


def test_planning_agent_extracts_real_capabilities(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "strategist", "planning", ["planning"],
                           bind=True)
        result = plane.agent_run(
            session, "strategist", "debug the failing export and test it")
        assert result["allowed"] is True
        run = wait_result(plane, session, "strategist", result["run_id"])
        assert run["success"] is True
        assert "debugging" in run["output"]


def test_coding_agent_applies_real_changes(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "implementer", "coding", ["coding"],
                           bind=True)
        # ASSISTED semantics: the change set files a real approval,
        # the operator approves it, and the same run then applies.
        result = plane.agent_run(session, "implementer",
                                 "add CSV export to the project")
        assert result["allowed"] is True
        deadline = time.time() + 30.0
        approved = False
        while time.time() < deadline:
            state = plane.agent_run_result(session, "implementer",
                                           result["run_id"])
            if state["status"] in ("finished", "failed"):
                break
            if not approved and approve_all(client, headers) > 0:
                approved = True
            time.sleep(0.1)
        run = wait_result(plane, session, "implementer", result["run_id"])
        assert run["success"] is True, run
        assert "app.py" in run["files"]
        assert "tests/test_csv.py" in run["files"]
        assert "def export_csv" in (
            Path(plane.projects["demo"].root, "app.py")).read_text()


def test_agent_run_denied_fails_closed(tmp_path):
    plane = make_plane(tmp_path, start=True,
                       policy=run_policy(effect="DENY"))
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "scout", "research", ["research"],
                           bind=True)
        result = plane.agent_run(session, "scout", "count files")
        assert result["allowed"] is False
        assert result["run_id"] == ""
        assert plane.agent_runs(session, "scout")["runs"] == []


def test_agent_run_approval_round_trip(tmp_path):
    plane = make_plane(tmp_path, start=True,
                       policy=run_policy(effect="REQUIRE_APPROVAL"))
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "scout", "research", ["research"],
                           bind=True)
        first = plane.agent_run(session, "scout", "count files")
        assert first["allowed"] is False
        approval_id = first["approval_request_id"]
        decided = plane.decide_agent_run_approval(session, approval_id, True)
        second = plane.agent_run(session, "scout", "count files",
                                 approval_id=decided["token_id"])
        assert second["allowed"] is True
        run = wait_result(plane, session, "scout", second["run_id"])
        assert run["success"] is True
        replay = plane.agent_run(session, "scout", "count files",
                                 approval_id=decided["token_id"])
        assert replay["allowed"] is False
        assert replay["approval_request_id"] != approval_id


def test_agent_run_api(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=run_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        created = client.post("/api/v1/agents", headers=headers,
                              json={"name": "scout", "role": "research",
                                    "capabilities": ["research"],
                                    "bind": True})
        assert created.status_code == 200
        run = client.post("/api/v1/agents/scout/run", headers=headers,
                          json={"requirement": "count the python files"})
        assert run.status_code == 200
        run_id = run.json()["run_id"]
        deadline = time.time() + 30.0
        final = None
        while time.time() < deadline:
            state = client.get(
                f"/api/v1/agents/scout/runs/{run_id}",
                headers=headers).json()
            if state["status"] == "finished":
                final = state["run"]
                break
            assert state["status"] != "failed", state
            time.sleep(0.1)
        assert final is not None
        assert final["success"] is True
        assert client.post("/api/v1/agents/scout/run", headers=headers,
                           json={"requirement": ""}).status_code == 400
        spec_only = client.post("/api/v1/agents", headers=headers,
                                json={"name": "spec", "role": "coding",
                                      "capabilities": ["coding"]})
        assert spec_only.status_code == 200
        refused = client.post("/api/v1/agents/spec/run", headers=headers,
                              json={"requirement": "write something"})
        assert refused.status_code == 200
        run_id = refused.json()["run_id"]
        deadline = time.time() + 10.0
        state = None
        while time.time() < deadline:
            state = client.get(f"/api/v1/agents/spec/runs/{run_id}",
                               headers=headers).json()
            if state["status"] == "failed":
                break
            time.sleep(0.05)
        assert state is not None
        assert "without a bound executor" in state["error"]
        history = client.get("/api/v1/agents/scout/runs", headers=headers)
        assert history.status_code == 200
        assert len(history.json()["runs"]) >= 1
