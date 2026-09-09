"""A35 desktop control-plane + API integration tests.

The cockpit talks to the Desktop Agent exclusively through the versioned
API; these tests verify capabilities, permission previews, observation
state, gated actuation with the approval round-trip, task grants, and
cross-session isolation.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402

from forge.security.policy import PermissionPolicy, PermissionRule, Resource  # noqa: E402


def desktop_policy():
    return PermissionPolicy(rules=[PermissionRule(
        id=f"r{i}", resource=Resource.DESKTOP, operation=op,
        effect="ALLOW") for i, op in enumerate([
            "screenshot", "read_screen", "window", "process",
            "system_info", "file_access", "clipboard", "launch",
            "mouse_move", "mouse_click", "keyboard"])])


def _setup(tmp_path, *, profile="assisted"):
    plane = make_plane(tmp_path, start=False, policy=desktop_policy())
    make_repo(Path(plane.projects["demo"].root))
    return plane, make_client(plane)


def _headers(client, profile="assisted"):
    with client:
        _session, _token, headers = login(client, profile=profile)
    return headers


def test_capabilities_are_honest_and_profile_aware(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        headers = _headers(client)
        payload = client.get("/api/v1/desktop/capabilities",
                             headers=headers).json()
        assert payload["status"] == "simulation"
        assert "fake" in payload["provider"].lower()
        assert payload["profile"] == "assisted"
        capabilities = payload["capabilities"]
        assert len(capabilities) == 15
        by_action = {item["action"]: item for item in capabilities}
        assert by_action["screenshot"]["observation"] is True
        assert by_action["launch"]["observation"] is False
        assert all(item["executable"] for item in capabilities)


def test_observation_state_endpoint(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        headers = _headers(client)
        state = client.get("/api/v1/desktop/state", headers=headers).json()
        assert state["profile"] == "assisted"
        observations = state["observations"]
        assert observations["screenshot"]["executed"] is True
        assert observations["windows"]["executed"] is True
        assert observations["processes"]["executed"] is True
        assert observations["system"]["executed"] is True
        assert state["provider"]["healthy"] is True
        assert state["provider"]["provider"]["simulation"] is True


def test_check_evaluates_without_executing(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        headers = _headers(client)
        checked = client.post("/api/v1/desktop/check", headers=headers,
                              json={"action": "launch",
                                    "target": "notepad"}).json()
        assert checked["executed"] is False
        assert checked["decision"] == "REQUIRE_APPROVAL"
        assert checked["risk"] == "MEDIUM"
        # The provider state is untouched by the preview.
        assert plane.desktop.provider.snapshot()["window_count"] == 1


def test_act_round_trip_with_approval(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        headers = _headers(client)
        # Assisted actuation: approval required, nothing executed.
        result = client.post("/api/v1/desktop/act", headers=headers, json={
            "action": "launch", "target": "notepad",
            "reason": "open editor"}).json()
        assert result["approval_required"] is True
        assert result["executed"] is False
        assert result["approval_request_id"]
        approval_id = result["approval_request_id"]
        # The request is visible to this session only.
        approvals = client.get("/api/v1/desktop/approvals",
                               headers=headers).json()["approvals"]
        assert any(item["id"] == approval_id for item in approvals)
        # Approve through the API -> minted single-use token.
        approved = client.post(
            f"/api/v1/desktop/approvals/{approval_id}/approve",
            headers=headers, json={}).json()
        assert approved["token_id"]
        # Resubmit with the token: executed on the fake desktop.
        result = client.post("/api/v1/desktop/act", headers=headers, json={
            "action": "launch", "target": "notepad",
            "reason": "open editor",
            "approval_id": approved["token_id"]}).json()
        assert result["executed"] is True
        assert result["decision"] == "ALLOW"
        assert result["observation"]["app"] == "notepad"
        # The observation state now reflects the launched window.
        state = client.get("/api/v1/desktop/state", headers=headers).json()
        assert state["observations"]["active_window"]["observation"][
            "active_window"] == "notepad"


def test_deny_round_trip_and_token_single_use(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        headers = _headers(client)
        first = client.post("/api/v1/desktop/act", headers=headers, json={
            "action": "launch", "target": "calculator"}).json()
        approval_id = first["approval_request_id"]
        denied = client.post(
            f"/api/v1/desktop/approvals/{approval_id}/deny",
            headers=headers, json={}).json()
        assert denied["approval"]["status"] == "denied"
        result = client.post("/api/v1/desktop/act", headers=headers, json={
            "action": "launch", "target": "calculator"}).json()
        assert result["executed"] is False
        # Single-use token: approve once, use once.
        first = client.post("/api/v1/desktop/act", headers=headers, json={
            "action": "launch", "target": "calculator"}).json()
        approved = client.post(
            f"/api/v1/desktop/approvals/{first['approval_request_id']}"
            "/approve", headers=headers, json={}).json()
        result = client.post("/api/v1/desktop/act", headers=headers, json={
            "action": "launch", "target": "calculator",
            "approval_id": approved["token_id"]}).json()
        assert result["executed"] is True
        replay = client.post("/api/v1/desktop/act", headers=headers, json={
            "action": "launch", "target": "calculator",
            "approval_id": approved["token_id"]}).json()
        assert replay["executed"] is False


def test_hard_invariants_block_even_with_approval_token(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        headers = _headers(client)
        result = client.post("/api/v1/desktop/act", headers=headers, json={
            "action": "launch", "target": "sudo",
            "params": {"args": ["whoami"]}}).json()
        assert result["decision"] == "DENY"
        assert not result["executed"]
        assert any("privilege escalation" in reason
                   for reason in result["reasons"])


def test_task_grants_and_autonomous_profile(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        headers = _headers(client, profile="autonomous")
        granted = client.post("/api/v1/desktop/grants", headers=headers,
                              json={"task_id": "task-9",
                                    "scopes": ["input"]}).json()
        assert granted["task_id"] == "task-9"
        result = client.post("/api/v1/desktop/act", headers=headers, json={
            "action": "mouse_move", "params": {"x": 10, "y": 20},
            "task_id": "task-9"}).json()
        assert result["executed"] is True
        assert result["decision"] == "ALLOW"
        # Same action under a task without a grant: denied.
        denied = client.post("/api/v1/desktop/act", headers=headers, json={
            "action": "mouse_move", "params": {"x": 10, "y": 20},
            "task_id": "task-none"}).json()
        assert denied["decision"] == "DENY"
        # Unknown scope rejected at the boundary.
        invalid = client.post("/api/v1/desktop/grants", headers=headers,
                              json={"task_id": "t", "scopes": ["moon"]})
        assert invalid.status_code == 400


def test_cross_session_isolation_of_approvals(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        headers_alice = _headers(client, profile="assisted")
        result = client.post("/api/v1/desktop/act", headers=headers_alice,
                             json={"action": "launch",
                                   "target": "notepad"}).json()
        approval_id = result["approval_request_id"]
        # Bob's session cannot see or decide Alice's desktop approval.
        _session, _token, headers_bob = login(client, actor="bob")
        visible = client.get("/api/v1/desktop/approvals",
                             headers=headers_bob).json()["approvals"]
        assert all(item["id"] != approval_id for item in visible)
        decided = client.post(
            f"/api/v1/desktop/approvals/{approval_id}/approve",
            headers=headers_bob, json={})
        assert decided.status_code == 404


def test_unknown_actions_and_auth_required(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        headers = _headers(client)
        invalid = client.post("/api/v1/desktop/act", headers=headers,
                              json={"action": "format_disk"})
        assert invalid.status_code == 400
        # Drop the session cookie: unauthenticated access must fail closed.
        client.cookies.clear()
        assert client.get("/api/v1/desktop/state").status_code == 401
        assert client.post("/api/v1/desktop/act",
                           json={"action": "screenshot"}).status_code == 401
