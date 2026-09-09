"""Agent skills (A54): validated declarative skill definitions."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402


def test_skill_validation(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        with pytest.raises(Exception):
            plane.skill_create(session, "Bad Name!", "coding")
        with pytest.raises(Exception):
            plane.skill_create(session, "good-name", "mind-reading")
        skill = plane.skill_create(
            session, "csv-writer", "documentation",
            description="writes csv exports")
        assert skill["declarative"] is True
        assert "grant" in skill["note"].lower() or \
            "power" in skill["note"].lower()
        with pytest.raises(Exception):
            plane.skill_create(session, "csv-writer", "documentation")


def test_attach_detach_updates_capabilities(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "worker", "coding", ["coding"])
        plane.skill_create(session, "csv-writer", "documentation")
        attached = plane.agent_attach_skill(session, "worker", "csv-writer")
        assert "documentation" in attached["capabilities"]
        assert attached["skills"] == ["csv-writer"]
        with pytest.raises(Exception):
            plane.agent_attach_skill(session, "worker", "csv-writer")
        with pytest.raises(Exception):
            plane.agent_attach_skill(session, "worker", "ghost")
        detached = plane.agent_detach_skill(session, "worker", "csv-writer")
        assert "documentation" not in detached["capabilities"]
        assert detached["skills"] == []
        with pytest.raises(Exception):
            plane.agent_detach_skill(session, "worker", "csv-writer")


def test_skill_attach_grants_no_execution_power(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        # An unbound agent stays unbound even with a skill attached.
        plane.agent_create(session, "spec", "documentation",
                           ["documentation"])
        plane.skill_create(session, "csv-writer", "documentation")
        attached = plane.agent_attach_skill(session, "spec", "csv-writer")
        assert attached["real"] is False
        assert attached["executor"] == ""
        # Built-in catalog unchanged.
        catalog = [item["name"] for item in plane.agent_catalog()]
        assert "spec" not in catalog


def test_skills_are_session_isolated(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        alice = plane.sessions.get(payload["session_id"])
        plane.skill_create(alice, "csv-writer", "documentation")
        _bs, _bt, _bh = login(client, actor="bob")
        bob = plane.sessions.get(_bs["session_id"])
        assert plane.skill_list(bob)["skills"] == []


def test_skills_api(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        created = client.post("/api/v1/skills", headers=headers,
                              json={"name": "csv-writer",
                                    "capability": "documentation"})
        assert created.status_code == 200
        assert created.json()["declarative"] is True
        assert client.post("/api/v1/skills", headers=headers,
                           json={"name": "bad", "capability": "nope"}
                           ).status_code == 400
        listed = client.get("/api/v1/skills", headers=headers)
        assert listed.status_code == 200
        assert [item["name"] for item in listed.json()["skills"]] == \
            ["csv-writer"]
        client.post("/api/v1/agents", headers=headers,
                    json={"name": "worker", "role": "coding",
                          "capabilities": ["coding"]})
        attached = client.post("/api/v1/agents/worker/skills",
                               headers=headers,
                               json={"skill": "csv-writer"})
        assert attached.status_code == 200
        assert "documentation" in attached.json()["capabilities"]
        detached = client.delete(
            "/api/v1/agents/worker/skills/csv-writer", headers=headers)
        assert detached.status_code == 200
        assert detached.json()["skills"] == []
