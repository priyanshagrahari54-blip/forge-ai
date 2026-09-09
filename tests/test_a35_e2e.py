"""A35 desktop E2E tests: the full controlled execution loop on the fake
desktop.

Observe -> plan -> authorize -> act -> verify, with an approval-gated
twin, state-preservation on denial, and disconnect/reconnect recovery.
All against the deterministic fake desktop; no real machine is touched.
"""
from __future__ import annotations

from forge.desktop.actions import DesktopRequest
from forge.desktop.agent import DesktopAgent
from forge.desktop.profiles import DesktopProfile
from forge.desktop.provider import FakeDesktopProvider
from forge.security.approvals import ApprovalStore
from forge.security.audit import AuditLog
from forge.security.policy import PermissionPolicy, PermissionRule, Resource


def policy():
    return PermissionPolicy(rules=[PermissionRule(
        id=f"r{i}", resource=Resource.DESKTOP, operation=op,
        effect="ALLOW") for i, op in enumerate([
            "screenshot", "read_screen", "window", "process",
            "system_info", "file_access", "clipboard", "launch",
            "mouse_move", "mouse_click", "keyboard"])])


def make_agent(provider=None, **kwargs):
    store = ApprovalStore()
    kwargs.setdefault("profile", DesktopProfile(mode="assisted"))
    instance = DesktopAgent(
        provider=provider or FakeDesktopProvider(),
        policy=policy(), store=store, audit=AuditLog(), **kwargs)
    return instance, store


def req(action, **kwargs):
    kwargs.setdefault("agent", "forge-desktop")
    return DesktopRequest(action, **kwargs)


def approve_next(store, actor="human"):
    pending = store.pending()[-1]
    store.decide(pending.id, True, decided_by=actor)
    return store.issue(pending.id, decided_by=actor, max_uses=1)


def test_full_observe_plan_act_verify_loop():
    provider = FakeDesktopProvider()
    instance, store = make_agent(provider)
    # Observe: what does the desktop look like?
    before = instance.observe(req("screenshot"))
    assert before.executed
    assert before.observation["focused"] == "desktop"
    # Plan: launch notepad (deterministic plan, actuation needs approval).
    plan = instance.act(req("launch", target="notepad",
                            reason="open the editor"))
    assert plan.approval_required and not plan.executed
    # Approve + act with the minted token.
    token = approve_next(store)
    acted = instance.act(req("launch", target="notepad",
                             reason="open the editor"),
                         approval_token_id=token.id)
    assert acted.executed and acted.observation["pid"] == 5000
    # Verify: the observation changed accordingly.
    after = instance.observe(req("screenshot"))
    assert after.observation["focused"] == "notepad"
    active = instance.observe(req("read_screen"))
    assert active.observation["active_window"] == "notepad"
    # Typing into the focused window: assisted profile -> approval.
    typing = instance.act(req("keyboard", target="notepad",
                              params={"text": "hello"},
                              reason="type a draft"))
    assert typing.approval_required
    token = approve_next(store)
    typed = instance.act(req("keyboard", target="notepad",
                             params={"text": "hello"},
                             reason="type a draft"),
                         approval_token_id=token.id)
    assert typed.executed
    # Verify the input landed.
    events = provider.input_log
    assert any(event["action"] == "keyboard" and
               event["params"].get("text") == "hello"
               for event in events)
    # Clipboard round-trip through the same pipeline.
    write_request = instance.act(req("clipboard",
    params={"op": "write",
    "content": "draft"}))
    assert write_request.approval_required
    written = instance.act(
    req("clipboard", params={"op": "write", "content": "draft"}),
    approval_token_id=approve_next(store).id)
    assert written.executed
    read_request = instance.act(req("clipboard",
    params={"op": "read"}))
    assert read_request.approval_required
    read = instance.act(req("clipboard", params={"op": "read"}),
    approval_token_id=approve_next(store).id)
    assert read.executed and read.observation["content"] == "draft"


def test_denied_action_leaves_desktop_state_untouched():
    provider = FakeDesktopProvider()
    instance, _store = make_agent(provider)
    snapshot_before = provider.snapshot()
    result = instance.act(req("launch", target="sudo",
                              params={"args": ["whoami"]}))
    assert result.decision == "DENY" and not result.executed
    # No window, no process, no input event, no clipboard change.
    assert provider.snapshot() == snapshot_before


def test_approval_denial_prevents_execution_and_tracks_audit():
    provider = FakeDesktopProvider()
    instance, store = make_agent(provider)
    instance.act(req("launch", target="calculator"))
    pending = store.pending()[-1]
    store.decide(pending.id, False, decided_by="human")
    result = instance.act(req("launch", target="calculator"))
    assert result.approval_required and not result.executed
    assert provider.snapshot()["window_count"] == 1  # untouched


def test_autonomous_profile_executes_low_risk_in_granted_scope():
    provider = FakeDesktopProvider()
    instance, store = make_agent(
        provider, profile=DesktopProfile(mode="autonomous"))
    instance.scope_checker.grant("task-7", ("input", "app:calculator"))
    # Low-risk actuation within scope: no approval prompt.
    move = instance.act(req("mouse_move", params={"x": 640, "y": 480},
                            task_id="task-7"))
    assert move.executed and move.decision == "ALLOW"
    # MEDIUM-risk actuation (launch) still needs approval even in scope.
    launch = instance.act(req("launch", target="calculator",
                              task_id="task-7"))
    assert launch.approval_required and not launch.executed
    # Out-of-scope actuation is denied outright.
    denied = instance.act(req("mouse_move", params={"x": 1, "y": 1},
                              task_id="task-other"))
    assert denied.decision == "DENY"
    # Grant expiry closes the window.
    instance.scope_checker._grants["task-7"]["expires"] = 0.0
    expired = instance.act(req("mouse_move", params={"x": 1, "y": 1},
                               task_id="task-7"))
    assert expired.decision == "DENY"


def test_provider_disconnect_recovery():
    provider = FakeDesktopProvider()
    instance, _store = make_agent(provider)
    provider.disconnect()
    failed = instance.observe(req("screenshot"))
    assert not failed.executed
    assert failed.error["kind"] == "disconnected"
    assert failed.recoverable
    # State is consistent: nothing executed while disconnected.
    provider.reconnect()
    recovered = instance.observe(req("screenshot"))
    assert recovered.executed


def test_bridge_session_boundary():
    from forge.desktop.bridge import DesktopBridge, DesktopBridgeError
    import pytest

    instance, store = make_agent()
    bridge = DesktopBridge(instance)
    session = bridge.open_session("alice", "demo")
    result = bridge.submit(session.bridge_id,
                           req("screenshot", agent="mallory"))
    # The bridge re-identifies the request as the session actor.
    assert result.executed
    bridge.close_session(session.bridge_id)
    with pytest.raises(DesktopBridgeError):
        bridge.submit(session.bridge_id, req("screenshot"))
    # simulate() only works on the fake provider.
    fake_session = bridge.open_session("bob", "demo")
    assert bridge.simulate(fake_session.bridge_id, width=1024, height=768)
    snapshot = bridge.snapshot(fake_session.bridge_id)
    assert snapshot["healthy"] and snapshot["provider"]["simulation"]
