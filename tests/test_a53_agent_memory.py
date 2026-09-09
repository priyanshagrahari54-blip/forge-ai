"""Agent memory (A53): durable, policy-gated per-agent memory."""
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

ALLOW_POLICY = PermissionPolicy(rules=[
    PermissionRule(id="m-w", resource=Resource.MEMORY, operation="write",
                   scope="", effect="ALLOW"),
    PermissionRule(id="m-r", resource=Resource.MEMORY, operation="read",
                   scope="", effect="ALLOW"),
    PermissionRule(id="m-d", resource=Resource.MEMORY, operation="delete",
                   scope="", effect="ALLOW"),
])

DENY_POLICY = PermissionPolicy(rules=[
    PermissionRule(id="m-w", resource=Resource.MEMORY, operation="write",
                   scope="", effect="DENY"),
    PermissionRule(id="m-r", resource=Resource.MEMORY, operation="read",
                   scope="", effect="DENY"),
])


def make_agent(plane, session, name="worker"):
    plane.agent_create(session, name, "research", ["research"],
                       bind=True)


def test_memory_roundtrip_and_persistence(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=ALLOW_POLICY)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        make_agent(plane, session)
        set_result = plane.agent_memory_set(
            session, "worker", "preference", "terse answers")
        assert set_result["allowed"] is True
        got = plane.agent_memory_get(session, "worker", "preference")
        assert got["entry"]["value"] == "terse answers"
        listed = plane.agent_memory_list(session, "worker")
        assert [entry["key"] for entry in listed["entries"]] == \
            ["preference"]
    # A brand-new plane over the same database still sees the memory.
    plane2 = make_plane(tmp_path, start=True, policy=ALLOW_POLICY)
    client2 = make_client(plane2)
    with client2:
        _s2, _t2, h2 = login(client2)
        session2 = plane2.sessions.get(_s2["session_id"])
        plane2.agent_create(session2, "worker", "research", ["research"],
                            bind=True)
        got = plane2.agent_memory_get(session2, "worker", "preference")
        assert got["entry"]["value"] == "terse answers"


def test_memory_denied_fails_closed(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=DENY_POLICY)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        make_agent(plane, session)
        set_result = plane.agent_memory_set(
            session, "worker", "secret", "value")
        assert set_result["allowed"] is False
        got = plane.agent_memory_get(session, "worker", "secret")
        assert got["allowed"] is False
        assert plane.agent_memory_list(
            session, "worker")["allowed"] is False


def test_memory_bounds_and_unknown_agent(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=ALLOW_POLICY)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        make_agent(plane, session)
        value = "x" * 3000
        set_result = plane.agent_memory_set(session, "worker", "big",
                                            value)
        assert set_result["entry"]["value"] == "x" * 2000
        with pytest.raises(Exception):
            plane.agent_memory_set(session, "ghost", "k", "v")
        deleted = plane.agent_memory_delete(session, "worker", "big")
        assert deleted["deleted"] is True
        again = plane.agent_memory_delete(session, "worker", "big")
        assert again["deleted"] is False


def test_memory_limit_is_real(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=ALLOW_POLICY)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        make_agent(plane, session)
        store = plane._agent_memory_store()
        # Fill the per-agent cap through the real store, then refuse.
        for index in range(200):
            store.set("worker", f"key-{index}", "v")
        with pytest.raises(Exception):
            plane.agent_memory_set(session, "worker", "overflow", "v")


def test_agent_memory_api(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=ALLOW_POLICY)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        created = client.post("/api/v1/agents", headers=headers,
                              json={"name": "worker", "role": "research",
                                    "capabilities": ["research"],
                                    "bind": True})
        assert created.status_code == 200
        set_result = client.post("/api/v1/agents/worker/memory",
                                 headers=headers,
                                 json={"key": "preference",
                                       "value": "terse"})
        assert set_result.status_code == 200
        assert set_result.json()["allowed"] is True
        got = client.get("/api/v1/agents/worker/memory/preference",
                         headers=headers)
        assert got.status_code == 200
        assert got.json()["entry"]["value"] == "terse"
        listed = client.get("/api/v1/agents/worker/memory", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["entries"]
        removed = client.delete("/api/v1/agents/worker/memory/preference",
                                headers=headers)
        assert removed.status_code == 200
        assert removed.json()["deleted"] is True
        assert client.post("/api/v1/agents/ghost/memory", headers=headers,
                           json={"key": "k", "value": "v"}
                           ).status_code == 400
