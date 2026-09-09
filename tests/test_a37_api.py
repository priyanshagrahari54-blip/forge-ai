"""Memory API integration tests (A37).

Every memory access passes the A33 permission gate at the API boundary:
policy-filtered overviews, approval round trips with single-use tokens,
session scoping, project memory, and clean error boundaries.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402

from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)


def memory_policy(write="REQUIRE_APPROVAL", read="ALLOW",
                  delete="ALLOW") -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="m-read", resource=Resource.MEMORY,
                       operation="read", scope="", effect=read),
        PermissionRule(id="m-write", resource=Resource.MEMORY,
                       operation="write", scope="", effect=write),
        PermissionRule(id="m-delete", resource=Resource.MEMORY,
                       operation="delete", scope="", effect=delete),
    ])


def _setup(tmp_path, policy=None):
    plane = make_plane(tmp_path, start=False,
                       policy=policy or memory_policy())
    make_repo(Path(plane.projects["demo"].root))
    return plane, make_client(plane)


def test_overview_and_round_trip_with_approval(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        overview = client.get("/api/v1/memory", headers=headers).json()
        assert overview == {"session_entries": [], "project_keys": []}
        # Write requires approval.
        first = client.post("/api/v1/memory", headers=headers, json={
            "kind": "note", "content": "api memory entry"}).json()
        assert first["allowed"] is False
        assert first["approval_required"] is True
        approval_id = first["approval_request_id"]
        visible = client.get("/api/v1/memory/approvals",
                             headers=headers).json()["approvals"]
        assert any(item["id"] == approval_id for item in visible)
        decided = client.post(
            f"/api/v1/memory/approvals/{approval_id}/approve",
            headers=headers, json={}).json()
        assert decided["token_id"]
        executed = client.post("/api/v1/memory", headers=headers, json={
            "kind": "note", "content": "api memory entry",
            "approval_id": decided["token_id"]}).json()
        assert executed["allowed"] is True
        entry_id = executed["entry"]["id"]
        overview = client.get("/api/v1/memory", headers=headers).json()
        assert [item["id"] for item in overview["session_entries"]] \
            == [entry_id]
        full = client.get(f"/api/v1/memory/entries/{entry_id}",
                          headers=headers).json()
        assert full["entry"]["content"] == "api memory entry"
        deleted = client.post("/api/v1/memory/delete", headers=headers,
                              json={"entry_id": entry_id}).json()
        assert deleted["allowed"] is True


def test_token_single_use_and_deny_round_trip(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        first = client.post("/api/v1/memory", headers=headers, json={
            "kind": "fact", "content": "first"}).json()
        approved = client.post(
            f"/api/v1/memory/approvals/{first['approval_request_id']}"
            "/approve", headers=headers, json={}).json()
        executed = client.post("/api/v1/memory", headers=headers, json={
            "kind": "fact", "content": "first",
            "approval_id": approved["token_id"]}).json()
        assert executed["allowed"] is True
        replay = client.post("/api/v1/memory", headers=headers, json={
            "kind": "fact", "content": "second",
            "approval_id": approved["token_id"]}).json()
        assert replay["allowed"] is False
        assert replay["approval_request_id"] != first["approval_request_id"]
        denied = client.post(
            f"/api/v1/memory/approvals/{replay['approval_request_id']}/deny",
            headers=headers, json={}).json()
        assert denied["approval"]["status"] == "denied"


def test_project_memory_api(tmp_path):
    plane, client = _setup(tmp_path, policy=memory_policy(write="ALLOW"))
    with client:
        _, _, headers = login(client)
        saved = client.post("/api/v1/memory/project/save", headers=headers,
                            json={"key": "facts/api",
                                  "content": "project level"}).json()
        assert saved["allowed"] is True
        loaded = client.get("/api/v1/memory/project/get?key=facts/api",
                            headers=headers).json()
        assert loaded["content"] == "project level"
        listed = client.get("/api/v1/memory/project",
                            headers=headers).json()
        assert "facts/api" in listed["project_keys"]
        # Traversal is rejected at the boundary.
        bad = client.post("/api/v1/memory/project/save", headers=headers,
                          json={"key": "../escape", "content": "x"})
        assert bad.status_code == 400


def test_cross_session_and_policy_isolation(tmp_path):
    plane, client = _setup(tmp_path, policy=memory_policy(write="ALLOW"))
    with client:
        _, _, alice = login(client)
        client.post("/api/v1/memory", headers=alice, json={
            "kind": "note", "content": "alice only"})
        _session, _token, bob = login(client, actor="bob")
        overview = client.get("/api/v1/memory", headers=bob).json()
        assert overview["session_entries"] == []
        # Bob cannot fetch Alice's entry id even by guessing.
        entry_id = client.get("/api/v1/memory",
                              headers=alice).json()["session_entries"][0]["id"]
        assert client.get(f"/api/v1/memory/entries/{entry_id}",
                          headers=bob).status_code == 404
        # A deny policy blocks even the owner.
        plane.policy = PermissionPolicy(rules=[
            PermissionRule(id="m-deny", resource=Resource.MEMORY,
                           operation="read", scope="", effect="DENY"),
        ])
        denied = client.get("/api/v1/memory", headers=alice).json()
        assert denied["session_entries"] == []
        assert denied["project_keys"] == []


def test_auth_and_bounds(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        assert client.get("/api/v1/memory").status_code == 401
        assert client.post("/api/v1/memory",
                           json={"kind": "note",
                                 "content": "x"}).status_code == 401
        _, _, headers = login(client)
        oversized = client.post("/api/v1/memory", headers=headers,
                                json={"kind": "note",
                                      "content": "x" * 20001})
        assert oversized.status_code == 400
        bad_kind = client.post("/api/v1/memory", headers=headers,
                               json={"kind": "password", "content": "x"})
        assert bad_kind.status_code == 400
        unknown = client.get("/api/v1/memory/entries/missing",
                             headers=headers)
        assert unknown.status_code == 404


def test_memory_mutations_are_audited(tmp_path):
    plane, client = _setup(tmp_path, policy=memory_policy(write="ALLOW"))
    with client:
        _, _, headers = login(client)
        client.post("/api/v1/memory", headers=headers,
                    json={"kind": "note", "content": "audited"})
        assert any(event.resource == "memory" and event.operation == "write"
                   for event in plane.audit.events)
