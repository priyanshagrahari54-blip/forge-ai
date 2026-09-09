"""Persistent sessions + memory across backend restarts (A37).

The cockpit database is the single durable source: sessions, session
tokens, active-task bindings, session memory, project memory, and run
records survive a brand-new ControlPlane over the same database.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import login, make_client, make_repo  # noqa: E402

from forge.control.control_plane import ControlConfig, ControlPlane  # noqa: E402
from forge.models.fabric import ModelFabric  # noqa: E402
from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)


def memory_policy() -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="m-read", resource=Resource.MEMORY,
                       operation="read", scope="", effect="ALLOW"),
        PermissionRule(id="m-write", resource=Resource.MEMORY,
                       operation="write", scope="", effect="ALLOW"),
    ])


def make_config(tmp_path):
    root = tmp_path / "demo"
    root.mkdir(parents=True, exist_ok=True)
    make_repo(root)
    return ControlConfig(
        db_path=str(tmp_path / "cockpit.db"),
        projects={"demo": str(root)},
        policy=memory_policy(), fabric=ModelFabric.from_defaults())


def test_session_and_token_survive_restart(tmp_path):
    config = make_config(tmp_path)
    plane = ControlPlane(config)
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session_id = payload["session_id"]
        plane.memory_add(plane.sessions.get(session_id), "fact",
                         "durable fact")
    # Brand-new backend over the same database.
    plane2 = ControlPlane(config)
    client2 = make_client(plane2)
    with client2:
        assert plane2.sessions.get(session_id) is not None
        # The old bearer token still authenticates and its memory is there.
        overview = client2.get("/api/v1/memory", headers=headers).json()
        assert any(entry["kind"] == "fact"
                   for entry in overview["session_entries"])
        # The session itself is still served by the session API.
        assert client2.get("/api/v1/sessions/me",
                           headers=headers).status_code == 200


def test_project_memory_and_runs_survive_restart(tmp_path):
    config = make_config(tmp_path)
    plane = ControlPlane(config)
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.memory_project_save(session, "facts/durable", "still here")
        run = plane.submit_task(session, "durable run")
        plane._record_outcome(run.id, {"accepted": True})
    plane2 = ControlPlane(config)
    client2 = make_client(plane2)
    with client2:
        overview = client2.get("/api/v1/memory", headers=headers).json()
        assert "facts/durable" in overview["project_keys"]
        loaded = client2.get(
            "/api/v1/memory/project/get?key=facts/durable",
            headers=headers).json()
        assert loaded["content"] == "still here"
        task = client2.get(f"/api/v1/tasks/{run.id}",
                           headers=headers)
        assert task.status_code == 200


def test_active_task_binding_survives_restart(tmp_path):
    config = make_config(tmp_path)
    plane = ControlPlane(config)
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        run = plane.submit_task(session, "keep me bound")
        assert plane.sessions.get(session.id).active_task == run.id
    plane2 = ControlPlane(config)
    with client:
        assert plane2.sessions.get(session.id).active_task == run.id


def test_session_expiry_still_enforced_after_restart(tmp_path):
    import time
    config = make_config(tmp_path)
    config.session_ttl = 0.05
    plane = ControlPlane(config)
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session_id = payload["session_id"]
    time.sleep(0.1)
    plane2 = ControlPlane(config)
    client2 = make_client(plane2)
    with client2:
        assert client2.get("/api/v1/memory", headers=headers).status_code == 401
        persisted = plane2.sessions.get(session_id)
        assert persisted is None or not persisted.active
