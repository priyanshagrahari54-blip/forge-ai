"""Agent packaging (A56): portable validated definitions."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402


def test_export_is_a_plain_spec(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "worker", "coding",
                           ["coding", "review"], description="exports")
        exported = plane.agent_export(session, "worker")
        assert exported["format"] == "forge-agent-definition"
        assert exported["name"] == "worker"
        assert exported["capabilities"] == ["coding", "review"]
        assert exported["generation"] == 1
        # Exports carry no secrets or executors.
        for key in exported:
            assert "secret" not in key.lower()
            assert "credential" not in key.lower()
        assert "executor" not in exported


def test_import_roundtrip_is_unbound(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        alice = plane.sessions.get(payload["session_id"])
        plane.agent_create(alice, "worker", "coding", ["coding"],
                           description="original")
        exported = plane.agent_export(alice, "worker")
        _bs, _bt, _bh = login(client, actor="bob")
        bob = plane.sessions.get(_bs["session_id"])
        imported = plane.agent_import(bob, exported)
        definition = imported["agent"]
        assert definition["name"] == "worker"
        assert definition["capabilities"] == ["coding"]
        assert definition["description"] == "original"
        # Imported definitions are never bound.
        assert definition["real"] is False
        assert definition["executor"] == ""



def test_import_validation_refuses_garbage(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        bad_cases = [
            {"name": "x", "role": "coding", "capabilities": ["coding"]},
            {"format": "forge-agent-definition", "format_version": 99,
             "name": "x", "role": "coding", "capabilities": ["coding"]},
            {"format": "forge-agent-definition", "format_version": 1,
             "name": "Bad Name!", "role": "coding",
             "capabilities": ["coding"]},
            {"format": "forge-agent-definition", "format_version": 1,
             "name": "good", "role": "coding",
             "capabilities": ["mind-reading"]},
            {"format": "forge-agent-definition", "format_version": 1,
             "name": "good", "role": "coding", "capabilities": "coding"},
        ]
        for case in bad_cases:
            with pytest.raises(Exception):
                plane.agent_import(session, case)
        assert plane.agent_definitions(session)["agents"] == []


def test_import_drops_unknown_skills_honestly(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.skill_create(session, "csv-writer", "documentation")
        exported = {
            "format": "forge-agent-definition", "format_version": 1,
            "name": "importer", "role": "coding",
            "capabilities": ["coding"],
            "skills": ["csv-writer", "ghost-skill"],
            "description": "", "generation": 1, "metrics": {},
            "created_by": "alice",
        }
        imported = plane.agent_import(session, exported)
        assert imported["agent"]["skills"] == ["csv-writer"]
        assert imported["dropped_skills"] == ["ghost-skill"]
        assert "documentation" in imported["agent"]["capabilities"]


def test_packaging_api(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        client.post("/api/v1/agents", headers=headers,
                    json={"name": "worker", "role": "coding",
                          "capabilities": ["coding"]})
        exported = client.get("/api/v1/agents/worker/export",
                              headers=headers)
        assert exported.status_code == 200
        payload = exported.json()
        _bs, _bt, bob_headers = login(client, actor="bob")
        imported = client.post("/api/v1/agents/import",
                               headers=bob_headers,
                               json={"payload": payload})
        assert imported.status_code == 200
        assert imported.json()["agent"]["real"] is False
        garbage = client.post("/api/v1/agents/import", headers=bob_headers,
                              json={"payload": {"name": "nope"}})
        assert garbage.status_code == 400
