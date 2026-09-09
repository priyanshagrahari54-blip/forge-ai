"""Computer-use control-plane tests (A40): observe/propose/act/cycle,
grants, modes, budget, escalation, isolation, redaction."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402
from helpers_a39 import b64, make_png  # noqa: E402

from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)


def computer_policy() -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="v-analyze", resource=Resource.VISION,
                       operation="analyze", scope="", effect="ALLOW"),
        PermissionRule(id="v-execute", resource=Resource.VISION,
                       operation="execute", scope="", effect="ALLOW"),
        PermissionRule(id="d-move", resource=Resource.DESKTOP,
                       operation="mouse_move", scope="", effect="ALLOW"),
        PermissionRule(id="d-click", resource=Resource.DESKTOP,
                       operation="mouse_click", scope="", effect="ALLOW"),
        PermissionRule(id="d-key", resource=Resource.DESKTOP,
                       operation="keyboard", scope="", effect="ALLOW"),
        PermissionRule(id="d-proc", resource=Resource.DESKTOP,
                       operation="process", scope="", effect="ALLOW"),
        PermissionRule(id="d-shot", resource=Resource.DESKTOP,
                       operation="screenshot", scope="", effect="ALLOW"),
    ])


def _plane(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=computer_policy())
    make_repo(Path(plane.projects["demo"].root))
    return plane, make_client(plane)


def _session(plane, client, profile="assisted"):
    payload, _token, headers = login(client, profile=profile)
    return plane.sessions.get(payload["session_id"]), headers


def _grant(client, headers, task_id, scopes):
    granted = client.post("/api/v1/desktop/grants", headers=headers,
                          json={"task_id": task_id, "scopes": scopes})
    assert granted.status_code == 200, granted.text
    return granted.json()


def test_observe_versions_snapshots_and_propose_dry_runs(tmp_path):
    plane, client = _plane(tmp_path)
    with client:
        session, headers = _session(plane, client)
        first = plane.computer_observe(session, b64(make_png()),
                                       goal="click OK")
        assert first["snapshot_version"] == 1
        assert first["element_tree"]["kind"] == "screen"
        second = plane.computer_observe(session, b64(make_png()),
                                        goal="click OK again")
        assert second["snapshot_version"] == 2
        proposed = plane.computer_propose(session, b64(make_png()),
                                          goal="click OK")
        assert proposed["executed"] is False
        assert proposed["proposals"]
        history = plane.computer_history(session)
        assert history["executed_actions"] == 0
        assert [s["version"] for s in history["snapshots"]] == [1, 2]


def test_act_executes_under_grant_and_policy(tmp_path):
    plane, client = _plane(tmp_path)
    with client:
        session, headers = _session(plane, client, profile="autonomous")
        task_id = session.id
        _grant(client, headers, task_id, ["input"])
        result = plane.computer_act(
            session, "mouse_move", params={"x": 10, "y": 20},
            reason="move to the button")
        assert result["executed"] is True
        assert result["allowed"] is True
        assert result["decision"] == "ALLOW"
        history = plane.computer_history(session)
        assert history["executed_actions"] == 1


def test_act_requires_approval_under_assisted(tmp_path):
    plane, client = _plane(tmp_path)
    with client:
        session, headers = _session(plane, client, profile="assisted")
        task_id = session.id
        _grant(client, headers, task_id, ["input"])
        result = plane.computer_act(
            session, "mouse_move", params={"x": 10, "y": 20},
            reason="move under assisted")
        assert result["executed"] is False
        assert result["approval_required"] is True
        approval_id = result["approval_request_id"]
        visible = plane.list_computer_approvals(session)
        assert any(item["id"] == approval_id for item in visible)
        decided = plane.decide_computer_approval(session, approval_id, True)
        executed = plane.computer_act(
            session, "mouse_move", params={"x": 10, "y": 20},
            reason="move with approval", approval_id=decided["token_id"])
        assert executed["executed"] is True
        # Single-use token: a second redemption fails closed.
        replay = plane.computer_act(
            session, "mouse_move", params={"x": 10, "y": 20},
            reason="replay", approval_id=decided["token_id"])
        assert replay["executed"] is False
        assert replay["approval_required"] is True


def test_safe_profile_denies_actuation(tmp_path):
    plane, client = _plane(tmp_path)
    with client:
        session, headers = _session(plane, client, profile="safe")
        task_id = session.id
        _grant(client, headers, task_id, ["input", "screen"])
        result = plane.computer_act(
            session, "mouse_move", params={"x": 10, "y": 20},
            reason="move under safe")
        assert result["allowed"] is False
        assert "observation only" in result["reason"]
        observation = plane.computer_act(session, "screenshot")
        assert observation["executed"] is True


def test_high_risk_action_escalates_even_autonomous(tmp_path):
    plane, client = _plane(tmp_path)
    with client:
        session, headers = _session(plane, client, profile="autonomous")
        task_id = session.id
        _grant(client, headers, task_id, ["process:*"])
        result = plane.computer_act(
            session, "process", target="browser",
            params={"op": "terminate"}, reason="stop the browser")
        assert result["allowed"] is False
        assert result["approval_required"] is True
        assert result["risk"] in ("HIGH", "CRITICAL")
        assert result["approval_request_id"]


def test_confirm_dialog_fails_closed_on_next_act(tmp_path):
    plane, client = _plane(tmp_path)
    with client:
        session, headers = _session(plane, client, profile="autonomous")
        task_id = session.id
        _grant(client, headers, task_id, ["input"])
        plane.computer_observe(
            session, b64(make_png(text_chunks=("Are you sure you want to "
                                               "delete this?",))),
            goal="confirm")
        result = plane.computer_act(
            session, "mouse_move", params={"x": 5, "y": 5},
            reason="move while a dialog is up")
        assert result["allowed"] is False
        assert "dialog" in result["reason"]
        # A fresh dialog-free screen clears the guard.
        plane.computer_observe(session, b64(make_png()), goal="fresh")
        cleared = plane.computer_act(
            session, "mouse_move", params={"x": 5, "y": 5},
            reason="move after dialog cleared")
        assert cleared["executed"] is True


def test_budget_is_enforced_per_task(tmp_path):
    from helpers_a34 import ScriptedProvider, make_fabric
    from forge.control.control_plane import ControlConfig, ControlPlane

    root = tmp_path / "demo"
    root.mkdir(parents=True, exist_ok=True)
    config = ControlConfig(
        db_path=str(tmp_path / "cockpit.db"),
        projects={"demo": str(root)},
        fabric=make_fabric(ScriptedProvider()),
        approval_timeout=60.0, approval_token_ttl=60.0,
        policy=computer_policy(), computer_max_actions=2)
    plane = ControlPlane(config)
    plane.start()
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        session, headers = _session(plane, client, profile="autonomous")
        task_id = session.id
        _grant(client, headers, task_id, ["input"])
        for _index in range(2):
            assert plane.computer_act(
                session, "mouse_move", params={"x": 1, "y": 1},
                reason="budgeted").get("executed") is True
        exhausted = plane.computer_act(
            session, "mouse_move", params={"x": 1, "y": 1},
            reason="over budget")
        assert exhausted["allowed"] is False
        assert "budget exhausted" in exhausted["reason"]


def test_history_redacts_typed_text(tmp_path):
    plane, client = _plane(tmp_path)
    with client:
        session, headers = _session(plane, client, profile="assisted")
        task_id = session.id
        _grant(client, headers, task_id, ["input"])
        plane.computer_act(
            session, "keyboard", target="editor",
            params={"text": "my super secret password 42"},
            reason="type credentials")
        history = plane.computer_history(session)
        assert "super secret" not in str(history)
        assert "<redacted" in str(history["actions"])


def test_cross_session_approval_isolation(tmp_path):
    plane, client = _plane(tmp_path)
    with client:
        alice, alice_headers = _session(plane, client, profile="assisted")
        task_id = alice.id
        _grant(client, alice_headers, task_id, ["input"])
        result = plane.computer_act(
            alice, "mouse_move", params={"x": 3, "y": 3},
            reason="needs approval")
        approval_id = result["approval_request_id"]
        assert approval_id
        _payload, _token, bob_headers = login(client, actor="bob")
        bob = plane.sessions.get(_payload["session_id"])
        assert plane.list_computer_approvals(bob) == []
        with pytest.raises(Exception):
            plane.decide_computer_approval(bob, approval_id, True)


def test_cycle_is_bounded_and_honest(tmp_path):
    plane, client = _plane(tmp_path)
    with client:
        session, headers = _session(plane, client, profile="autonomous")
        task_id = session.id
        _grant(client, headers, task_id, ["input"])
        payload = plane.computer_cycle(session, b64(make_png()),
                                       goal="click OK")
        assert payload["allowed"] is True
        assert "cycle" in payload["note"].lower()
        assert payload["observed"]["snapshot_version"] == 1
        assert plane.computer_history(session)["executed_actions"] <= 1


def test_audit_records_computer_events(tmp_path):
    plane, client = _plane(tmp_path)
    with client:
        session, _headers = _session(plane, client)
        plane.computer_observe(session, b64(make_png()), goal="audit")
        plane.computer_propose(session, b64(make_png()), goal="audit")
        assert any(getattr(event, "resource", "") == "computer"
                   for event in plane.audit.events)
