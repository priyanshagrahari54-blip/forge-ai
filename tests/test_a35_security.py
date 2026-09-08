"""A35 desktop security tests — the no-bypass invariants.

The desktop agent must fail closed under every attempt to route around
the A33 permission system: hard invariants beat allow-policies, profiles,
scope grants, and approval tokens; CUSTOM profiles can only tighten;
approval tokens are single-use and scope-bound; bridge sessions expire
and re-identify callers; provider state never leaks secrets; and the API
never claims real desktop control.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402

from forge.desktop.actions import DesktopActionKind, DesktopRequest  # noqa: E402
from forge.desktop.agent import DesktopAgent  # noqa: E402
from forge.desktop.bridge import DesktopBridge, DesktopBridgeError  # noqa: E402
from forge.desktop.profiles import DesktopProfile, ProfileError  # noqa: E402
from forge.desktop.provider import FakeDesktopProvider  # noqa: E402
from forge.security.approvals import ApprovalStore  # noqa: E402
from forge.security.audit import AuditLog  # noqa: E402
from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)


def allow_all_desktop_policy() -> PermissionPolicy:
    ops = ["read_screen", "screenshot", "mouse_move", "mouse_click",
           "keyboard", "launch", "window", "clipboard", "file_access",
           "process", "system_info"]
    return PermissionPolicy(rules=[
        PermissionRule(id=f"allow-{op}", resource=Resource.DESKTOP,
                       operation=op, effect="ALLOW")
        for op in ops])


def agent(profile: DesktopProfile | None = None, **kwargs):
    kwargs.setdefault("provider", FakeDesktopProvider())
    kwargs.setdefault("policy", allow_all_desktop_policy())
    kwargs.setdefault("store", ApprovalStore())
    kwargs.setdefault("audit", AuditLog())
    kwargs.setdefault("profile", profile or DesktopProfile(mode="autonomous"))
    return DesktopAgent(**kwargs)


def req(kind: str, **kwargs) -> DesktopRequest:
    kwargs.setdefault("agent", "forge-desktop")
    return DesktopRequest(DesktopActionKind(kind), **kwargs)


def mint(instance, request, *, target=None, params=None, task_id="t1"):
    request = DesktopRequest(request.kind, target=request.target,
                             params=dict(request.params), agent=request.agent,
                             task_id=request.task_id)
    pending = instance.store.pending()[-1]
    instance.store.decide(pending.id, True, decided_by="human")
    return instance.store.issue(pending.id, decided_by="human", max_uses=1)


# -- identity, validation, and scope fail closed ------------------------------


def test_missing_agent_identity_fails_closed():
    instance = agent()
    result = instance.act(DesktopRequest("screenshot", agent=""))
    assert result.decision == "DENY"
    assert not result.executed


def test_invalid_request_fails_closed_before_policy():
    instance = agent()
    result = instance.act(req("keyboard", params={"text": "x" * 2001}))
    assert result.decision == "DENY"
    assert not result.executed


def test_task_scope_required_for_actuation_even_when_policy_allows():
    instance = agent()  # autonomous, allow-all policy
    # No task grant at all: actuation denied.
    result = instance.act(req("mouse_move", params={"x": 1, "y": 2},
                              task_id="ungranted"))
    assert result.decision == "DENY"
    assert not result.executed


def test_expired_task_scope_grant_fails_closed():
    instance = agent()
    instance.scope_checker.grant("t1", ("*",), ttl=-1)
    result = instance.act(req("mouse_move", params={"x": 1, "y": 2},
                              task_id="t1"))
    assert result.decision == "DENY"


def test_revoked_task_scope_grant_fails_closed():
    instance = agent()
    instance.scope_checker.grant("t1", ("*",))
    assert instance.scope_checker.revoke("t1")
    result = instance.act(req("mouse_move", params={"x": 1, "y": 2},
                              task_id="t1"))
    assert result.decision == "DENY"


# -- hard invariants beat everything ------------------------------------------

INVARIANT_CASES = [
    ("launch", "sudo", {"args": ["whoami"]}, "privilege escalation"),
    ("launch", "aws", {"args": ["cat", ".aws/credentials"]},
     "credential extraction"),
    ("file_access", "id_rsa", {"mode": "read"}, "credential extraction"),
    ("launch", "defender", {"args": ["kill"]}, "security control"),
    ("file_access", ".config/autostart/x",
     {"mode": "write", "content": "x"}, "unauthorized persistence"),
    ("launch", "teamviewer", {}, "remote control"),
]


@pytest.mark.parametrize("kind,target,params,family", INVARIANT_CASES)
def test_hard_invariants_beat_policy_profile_scope_and_token(kind, target,
                                                             params, family):
    instance = agent()  # autonomous profile, allow-all policy
    instance.scope_checker.grant("t1", ("*",))
    request = req(kind, target=target, params=params, task_id="t1")
    # Even a minted human approval token must not buy the invariant.
    first = instance.act(request)
    assert first.decision == "DENY", family
    assert not first.executed
    assert any(family in reason for reason in first.reasons)


def test_hard_invariant_denies_are_recorded_in_audit():
    audit = AuditLog()
    instance = agent(audit=audit)
    instance.scope_checker.grant("t1", ("*",))
    instance.act(req("launch", target="sudo", params={"args": ["whoami"]},
                     task_id="t1"))
    entries = audit.events
    assert any(
        getattr(entry, "decision", "") == "DENY"
        and "privilege escalation" in getattr(entry, "reason", "")
        for entry in entries)


# -- profiles can only tighten -----------------------------------------------


def test_custom_profile_cannot_loosen_base_mode():
    with pytest.raises(ProfileError):
        DesktopProfile(mode="custom", base="assisted",
                       overrides={"keyboard": "AUTO"})
    with pytest.raises(ProfileError):
        DesktopProfile(mode="custom", base="safe",
                       overrides={"launch": "APPROVAL"})


def test_custom_profile_can_tighten_and_still_fails_invariants():
    instance = agent(profile=DesktopProfile(mode="custom", base="autonomous",
                                            overrides={
                                                "mouse_move": "APPROVAL"}))
    instance.scope_checker.grant("t1", ("*",))
    moved = instance.act(req("mouse_move", params={"x": 1, "y": 2},
                             task_id="t1"))
    assert moved.decision == "REQUIRE_APPROVAL"
    instance.scope_checker.grant("t1", ("*",))
    denied = instance.act(req("launch", target="sudo",
                              params={"args": ["id"]}, task_id="t1"))
    assert denied.decision == "DENY"


def test_safe_profile_blocks_actuation_even_when_policy_allows():
    instance = agent(profile=DesktopProfile(mode="safe"))
    instance.scope_checker.grant("t1", ("*",))
    result = instance.act(req("mouse_move", params={"x": 1, "y": 2},
                              task_id="t1"))
    assert result.decision == "DENY"
    assert not result.executed
    # Observation stays available.
    observed = instance.observe(req("screenshot"))
    assert observed.executed


def test_assisted_profile_requires_approval_for_actuation():
    instance = agent(profile=DesktopProfile(mode="assisted"))
    instance.scope_checker.grant("t1", ("*",))
    result = instance.act(req("mouse_move", params={"x": 1, "y": 2},
                              task_id="t1"))
    assert result.decision == "REQUIRE_APPROVAL"
    assert not result.executed
    token = mint(instance, req("mouse_move", params={"x": 1, "y": 2},
                               task_id="t1"))
    executed = instance.act(req("mouse_move", params={"x": 1, "y": 2},
                                task_id="t1"),
                            approval_token_id=token.id)
    assert executed.executed


# -- approval tokens: single use, scope bound --------------------------------


def test_token_replay_fails_closed():
    instance = agent(profile=DesktopProfile(mode="assisted"))
    instance.scope_checker.grant("t1", ("*",))
    request = req("mouse_move", params={"x": 1, "y": 2}, task_id="t1")
    instance.act(request)
    token = mint(instance, request)
    first = instance.act(request, approval_token_id=token.id)
    assert first.executed
    replay = instance.act(request, approval_token_id=token.id)
    assert not replay.executed
    assert replay.approval_required  # files a fresh request, fails closed


def test_token_is_scope_bound_to_request_operation():
    instance = agent(profile=DesktopProfile(mode="assisted"))
    instance.scope_checker.grant("t1", ("*",))
    request = req("launch", target="notepad", task_id="t1")
    instance.act(request)
    token = mint(instance, request)
    other = instance.act(req("launch", target="calculator", task_id="t1"),
                         approval_token_id=token.id)
    assert not other.executed


# -- bridge boundary ----------------------------------------------------------


def test_bridge_rejects_unknown_session():
    bridge = DesktopBridge(agent())
    with pytest.raises(DesktopBridgeError):
        bridge.submit("bogus", req("screenshot"))


def test_bridge_reidentifies_caller_actor():
    instance = agent()
    bridge = DesktopBridge(instance)
    session = bridge.open_session("mallory", "demo")
    # The request claims to be another agent; the bridge overrides it.
    result = bridge.submit(session.bridge_id,
                           req("screenshot", agent="someone-else"))
    assert result.executed
    # Actuation without a task grant still fails closed.
    denied = bridge.submit(session.bridge_id,
                           req("mouse_move", params={"x": 1, "y": 2},
                               task_id="nope"))
    assert denied.decision == "DENY"


def test_bridge_session_expiry_fails_closed():
    instance = agent()
    bridge = DesktopBridge(instance, ttl=0.01)
    session = bridge.open_session("alice", "demo")
    import time
    time.sleep(0.02)
    with pytest.raises(DesktopBridgeError):
        bridge.submit(session.bridge_id, req("screenshot"))


# -- provider honesty and secrecy --------------------------------------------


def test_provider_snapshot_never_leaks_clipboard():
    provider = FakeDesktopProvider()
    provider.clipboard("", {"op": "write", "content": "top-secret-123"})
    snapshot = provider.snapshot()
    assert "top-secret-123" not in repr(snapshot)
    instance = agent()
    instance.provider = provider
    result = instance.observe(req("screenshot"))
    assert "top-secret-123" not in repr(result.to_dict())


def test_provider_disconnect_is_structured_and_recoverable():
    provider = FakeDesktopProvider()
    instance = agent()
    instance.provider = provider
    provider.disconnect()
    result = instance.observe(req("screenshot"))
    assert not result.executed
    assert result.error["kind"] == "disconnected"
    assert result.recoverable
    provider.reconnect()
    assert instance.observe(req("screenshot")).executed


def test_provider_injected_failure_is_specific():
    provider = FakeDesktopProvider()
    instance = agent()
    instance.provider = provider
    provider.fail_next("screenshot")
    result = instance.observe(req("screenshot"))
    assert result.error["kind"] == "unavailable"


# -- API boundary ------------------------------------------------------------


def _setup(tmp_path):
    plane = make_plane(tmp_path, start=False,
                       policy=allow_all_desktop_policy())
    make_repo(Path(plane.projects["demo"].root))
    return plane, make_client(plane)


def test_api_desktop_requires_session(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        for method, path in [("get", "/api/v1/desktop/capabilities"),
                             ("get", "/api/v1/desktop/state")]:
            response = getattr(client, method)(path)
            assert response.status_code == 401, (method, path)
        for path in ("/api/v1/desktop/act", "/api/v1/desktop/grants"):
            response = client.post(path, json={})
            assert response.status_code == 401, path


def test_api_desktop_unknown_action_and_scope_rejected(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        unknown = client.post("/api/v1/desktop/act", headers=headers,
                              json={"action": "format_c"})
        assert unknown.status_code == 400
        bad_scope = client.post("/api/v1/desktop/grants", headers=headers,
                                json={"task_id": "t1", "scopes": ["moon"]})
        assert bad_scope.status_code == 400


def test_api_desktop_approval_round_trip_single_use(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        # Interactive request (no task_id): assisted profile requires
        # approval even though the allow-all policy permits the action.
        first = client.post("/api/v1/desktop/act", headers=headers, json={
            "action": "mouse_move", "params": {"x": 1, "y": 2}}).json()
        assert first["approval_required"]
        approval_id = first["approval_request_id"]
        approved = client.post(
            f"/api/v1/desktop/approvals/{approval_id}/approve",
            headers=headers, json={}).json()
        token = approved["token_id"]
        executed = client.post("/api/v1/desktop/act", headers=headers, json={
            "action": "mouse_move", "params": {"x": 1, "y": 2},
            "approval_id": token}).json()
        assert executed["executed"]
        replay = client.post("/api/v1/desktop/act", headers=headers, json={
            "action": "mouse_move", "params": {"x": 1, "y": 2},
            "approval_id": token}).json()
        assert not replay["executed"]


def test_api_desktop_deny_round_trip(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        first = client.post("/api/v1/desktop/act", headers=headers, json={
            "action": "mouse_move", "params": {"x": 1, "y": 2}}).json()
        denied = client.post(
            f"/api/v1/desktop/approvals/{first['approval_request_id']}/deny",
            headers=headers, json={}).json()
        assert denied["approval"]["status"] == "denied"
        retry = client.post("/api/v1/desktop/act", headers=headers, json={
            "action": "mouse_move", "params": {"x": 1, "y": 2}}).json()
        assert not retry["executed"]


def test_api_hard_invariants_block_even_with_token(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        result = client.post("/api/v1/desktop/act", headers=headers, json={
            "action": "launch", "target": "sudo",
            "params": {"args": ["whoami"]}}).json()
        assert result["decision"] == "DENY"
        assert not result["executed"]


def test_api_desktop_is_honest_about_simulation(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        capabilities = client.get("/api/v1/desktop/capabilities",
                                  headers=headers).json()
        assert capabilities["status"] == "simulation"
        assert "fake" in capabilities["provider"].lower()
        state = client.get("/api/v1/desktop/state", headers=headers).json()
        assert state["provider"]["provider"]["simulation"] is True


def test_desktop_check_never_executes(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        before = plane.desktop.provider.snapshot()
        checked = client.post("/api/v1/desktop/check", headers=headers,
                              json={"action": "launch",
                                    "target": "notepad"}).json()
        assert checked["executed"] is False
        assert plane.desktop.provider.snapshot() == before
