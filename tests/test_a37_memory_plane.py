"""Memory control-plane behavior (A37): policy gating, approvals, scoping.

Memory is just another policy-gated surface: DENY blocks, ALLOW
proceeds, REQUIRE_APPROVAL files a session-bound request and redeems
single-use tokens. Sessions never see each other's memory, project
memory is path-safe, and run outcomes are recorded as durable bounded
summaries.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402

from forge.control.control_plane import ControlConfig, ControlPlane  # noqa: E402
from forge.control.memory import SessionMemoryStore  # noqa: E402
from forge.models.fabric import ModelFabric  # noqa: E402
from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)


def memory_policy(*, write="ALLOW", read="ALLOW",
                  delete="ALLOW") -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="m-read", resource=Resource.MEMORY,
                       operation="read", scope="", effect=read),
        PermissionRule(id="m-write", resource=Resource.MEMORY,
                       operation="write", scope="", effect=write),
        PermissionRule(id="m-delete", resource=Resource.MEMORY,
                       operation="delete", scope="", effect=delete),
    ])


def _plane(tmp_path, policy=None):
    root = tmp_path / "demo"
    root.mkdir(parents=True, exist_ok=True)
    config = ControlConfig(
        db_path=str(tmp_path / "cockpit.db"),
        projects={"demo": str(root)},
        policy=policy or memory_policy(),
        fabric=ModelFabric.from_defaults())
    plane = ControlPlane(config)
    make_repo(root)
    return plane


def _session(plane, client, actor="alice"):
    payload, _token, headers = login(client, actor=actor)
    return plane.sessions.get(payload["session_id"]), headers


def test_add_list_get_delete_flow(tmp_path):
    plane = _plane(tmp_path)
    client = make_client(plane)
    with client:
        session, _headers = _session(plane, client)
        saved = plane.memory_add(session, "note", "keep this note")
        assert saved["allowed"]
        entry_id = saved["entry"]["id"]
        overview = plane.memory_overview(session)
        assert [e["id"] for e in overview["session_entries"]] == [entry_id]
        loaded = plane.memory_get(session, entry_id)
        assert loaded["entry"]["content"] == "keep this note"
        assert plane.memory_delete(session, entry_id)["allowed"]
        assert plane.memory_overview(session)["session_entries"] == []


def test_deny_policy_blocks_memory_access(tmp_path):
    plane = _plane(tmp_path, policy=memory_policy(write="DENY",
                                                  read="DENY"))
    client = make_client(plane)
    with client:
        session, _headers = _session(plane, client)
        with pytest.raises(Exception) as info:
            plane.memory_add(session, "note", "nope")
        assert "deny" in str(info.value).lower()
        assert plane.session_memory.list(session.id) == []


def test_approval_round_trip_with_single_use_token(tmp_path):
    plane = _plane(tmp_path, policy=memory_policy(write="REQUIRE_APPROVAL"))
    client = make_client(plane)
    with client:
        session, _headers = _session(plane, client)
        first = plane.memory_add(session, "fact", "needs approval")
        assert not first["allowed"]
        assert first["approval_required"]
        approval_id = first["approval_request_id"]
        assert any(item["id"] == approval_id
                   for item in plane.list_memory_approvals(session))
        # The session actor decides; the store enforces approver != agent.
        decided = plane.decide_memory_request(session, approval_id, True)
        executed = plane.memory_add(
            session, "fact", "needs approval",
            approval_id=decided["token_id"])
        assert executed["allowed"]
        # Replay: single-use token fails closed into a fresh approval.
        replay = plane.memory_add(
            session, "fact", "again", approval_id=decided["token_id"])
        assert not replay["allowed"]
        assert replay["approval_required"]
        assert replay["approval_request_id"] != approval_id


def test_cross_session_isolation(tmp_path):
    plane = _plane(tmp_path)
    client = make_client(plane)
    with client:
        alice, _ha = _session(plane, client, actor="alice")
        plane.memory_add(alice, "note", "alice secret")
        bob, _hb = _session(plane, client, actor="bob")
        assert plane.memory_overview(bob)["session_entries"] == []
        entry_id = plane.session_memory.list(alice.id)[0].id
        with pytest.raises(Exception):
            plane.memory_get(bob, entry_id)
        with pytest.raises(Exception):
            plane.memory_delete(bob, entry_id)
        assert len(plane.session_memory.list(alice.id)) == 1


def test_project_memory_path_safety_and_redaction(tmp_path):
    plane = _plane(tmp_path)
    client = make_client(plane)
    with client:
        session, _headers = _session(plane, client)
        assert plane.memory_project_save(
            session, "facts/deploy", "main branch only")["allowed"]
        loaded = plane.memory_project_load(session, "facts/deploy")
        assert loaded["content"] == "main branch only"
        assert "facts/deploy" in plane.memory_overview(
            session)["project_keys"]
        with pytest.raises(Exception):
            plane.memory_project_save(session, "../escape", "x")
        with pytest.raises(Exception):
            plane.memory_project_load(session, "../escape")
        # Missing keys load as None without raising (no traversal involved).
        assert plane.memory_project_load(session, "missing/key")[
            "content"] is None


def test_run_outcomes_recorded_as_durable_memory(tmp_path):
    plane = _plane(tmp_path)
    client = make_client(plane)
    with client:
        session, headers = _session(plane, client)
        run = plane.submit_task(session, "do the demo work")
        outcome = {"accepted": True, "files": ["src/a.py"],
                   "model": "scripted", "provider": "local",
                   "report": {"summary": "ok"}}
        plane._record_outcome(run.id, outcome)
        store = plane._project_memory("demo")
        key = f"runs/{run.id}"
        recorded = json.loads(store.load(key))
        assert recorded["status"] == "SUCCEEDED"
        assert recorded["requirement"] == "do the demo work"
        assert recorded["model"] == "scripted"
        # Bounded retention: old summaries are pruned.
        for index in range(60):
            other = plane.submit_task(session, f"work {index}")
            plane._record_outcome(other.id, {"accepted": False})
        run_keys = [k for k in store.list() if k.startswith("runs/")]
        assert len(run_keys) == 50
        assert key not in run_keys  # oldest went first


def test_memory_record_runs_can_be_disabled(tmp_path):
    root = tmp_path / "demo"
    root.mkdir(parents=True, exist_ok=True)
    config = ControlConfig(
        db_path=str(tmp_path / "cockpit.db"),
        projects={"demo": str(root)},
        policy=memory_policy(), fabric=ModelFabric.from_defaults(),
        memory_record_runs=False)
    plane = ControlPlane(config)
    make_repo(root)
    client = make_client(plane)
    with client:
        session, _headers = _session(plane, client)
        run = plane.submit_task(session, "quiet work")
        plane._record_outcome(run.id, {"accepted": True})
        store = plane._project_memory("demo")
        assert store.list() == []


def test_invalid_memory_inputs_rejected(tmp_path):
    plane = _plane(tmp_path)
    client = make_client(plane)
    with client:
        session, _headers = _session(plane, client)
        with pytest.raises(Exception):
            plane.memory_add(session, "note", "")
        with pytest.raises(Exception):
            plane.memory_add(session, "password", "x")
        with pytest.raises(Exception):
            plane.memory_add(session, "note", "x" * 20001)
        with pytest.raises(Exception):
            plane.memory_project_save(session, "", "x")
        with pytest.raises(Exception):
            plane.memory_project_save(session, "k" * 257, "x")


def test_memory_policy_vocabulary_matches():
    from forge.security.policy import PermissionRequest
    policy = PermissionPolicy(rules=[
        PermissionRule(id="m-read", resource=Resource.MEMORY,
                       operation="read", scope="facts/deploy",
                       effect="ALLOW"),
        PermissionRule(id="m-write", resource=Resource.MEMORY,
                       operation="write", scope="", effect="DENY"),
    ])
    read = policy.evaluate(PermissionRequest(
        agent="forge-memory", resource=Resource.MEMORY, operation="read",
        scope="facts/deploy"))
    assert read.decision.value == "ALLOW"
    other = policy.evaluate(PermissionRequest(
        agent="forge-memory", resource=Resource.MEMORY, operation="read",
        scope="facts/other"))
    assert other.decision.value == "DENY"
    write = policy.evaluate(PermissionRequest(
        agent="forge-memory", resource=Resource.MEMORY, operation="write",
        scope="anything"))
    assert write.decision.value == "DENY"
    # Unknown memory operations fail closed.
    unknown = policy.evaluate(PermissionRequest(
        agent="forge-memory", resource=Resource.MEMORY, operation="grant",
        scope="x"))
    assert unknown.decision.value == "DENY"
