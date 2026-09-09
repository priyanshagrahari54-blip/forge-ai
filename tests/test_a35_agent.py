"""A35 DesktopAgent pipeline tests.

The pipeline order is contract: identity -> validation -> task scope ->
A33 policy -> risk + hard invariants -> profile -> approval -> execution
-> audit. These tests verify each layer and its interactions.
"""
from __future__ import annotations

from forge.desktop.actions import DesktopRequest
from forge.desktop.agent import DesktopAgent, GrantScopeChecker
from forge.desktop.profiles import DesktopProfile
from forge.desktop.provider import FakeDesktopProvider
from forge.security.approvals import ApprovalStore
from forge.security.audit import AuditLog
from forge.security.policy import (
    PermissionPolicy,
    PermissionRule,
    Resource,
)


def policy(*rules):
    return PermissionPolicy(rules=[PermissionRule(
        id=rule_id, resource=Resource.DESKTOP, operation=operation,
        effect=effect, scope=scope) for rule_id, operation, effect, scope
        in rules])


ALLOW_ALL = policy(
    ("r1", "screenshot", "ALLOW", ""), ("r2", "read_screen", "ALLOW", ""),
    ("r3", "window", "ALLOW", ""), ("r4", "process", "ALLOW", ""),
    ("r5", "system_info", "ALLOW", ""), ("r6", "file_access", "ALLOW", ""),
    ("r7", "clipboard", "ALLOW", ""), ("r8", "launch", "ALLOW", ""),
    ("r9", "mouse_move", "ALLOW", ""), ("r10", "mouse_click", "ALLOW", ""),
    ("r11", "keyboard", "ALLOW", ""))


def agent(**kwargs):
    kwargs.setdefault("provider", FakeDesktopProvider())
    kwargs.setdefault("policy", ALLOW_ALL)
    kwargs.setdefault("store", ApprovalStore())
    kwargs.setdefault("audit", AuditLog())
    return DesktopAgent(**kwargs)


def req(action, **kwargs):
    kwargs.setdefault("agent", "forge-desktop")
    return DesktopRequest(action, **kwargs)


def test_fail_closed_without_policy():
    bare = DesktopAgent(FakeDesktopProvider())
    result = bare.act(req("screenshot"))
    assert result.decision == "DENY"
    assert not result.executed


def test_missing_identity_denied():
    result = agent().act(DesktopRequest("screenshot", agent=""))
    assert result.decision == "DENY"
    assert any("identity" in reason
               for reason in result.to_dict()["reasons"])
    assert not result.executed


def test_invalid_request_denied_before_policy():
    result = agent().act(req("mouse_move", params={"x": -5, "y": 0}))
    assert result.decision == "DENY"
    assert not result.executed


def test_policy_deny_wins():
    restricted = agent(policy=policy(
        ("r8", "launch", "DENY", ""), ("r1", "screenshot", "ALLOW", "")))
    result = restricted.act(req("launch", target="notepad"))
    assert result.decision == "DENY"
    assert restricted.observe(req("screenshot")).executed


def test_hard_invariants_beat_allow_policy_and_tokens():
    from forge.security.approvals import ApprovalRequest

    store = ApprovalStore()
    instance = agent(store=store, profile=DesktopProfile(mode="autonomous"))
    instance.scope_checker.grant("t1", ("*",))
    # Even a pre-issued token cannot override a hard invariant.
    filed = store.submit(ApprovalRequest(
        agent="forge-desktop", resource=Resource.DESKTOP,
        operation="launch", scopes=("app:sudo",), task_id="t1",
        reason="test"))
    store.decide(filed.id, True, decided_by="admin")
    token = store.issue(filed.id, decided_by="admin", max_uses=5)
    result = instance.act(req("launch", target="sudo",
                              params={"args": ["whoami"]}, task_id="t1"),
                          approval_token_id=token.id)
    assert result.decision == "DENY"
    assert any("privilege escalation" in reason for reason in result.reasons)
    assert not result.executed


def test_observation_executes_without_approval():
    instance = agent()
    result = instance.observe(req("screenshot"))
    assert result.decision == "ALLOW"
    assert result.executed
    assert result.observation["focused"] == "desktop"
    # observe() refuses actuation kinds outright.
    blocked = instance.observe(req("keyboard", params={"text": "x"}))
    assert blocked.decision == "DENY"
    assert not blocked.executed


def test_assisted_actuation_files_approval_and_executes_with_token():
    store = ApprovalStore()
    instance = agent(store=store)
    result = instance.act(req("launch", target="notepad",
                              reason="open editor"))
    assert result.decision == "REQUIRE_APPROVAL"
    assert result.approval_required and result.approval_request_id
    assert not result.executed
    # The approval is a real A33 request with the right binding.
    pending = store.pending()
    assert len(pending) == 1
    assert pending[0].operation == "launch"
    assert pending[0].scopes == ("app:notepad",)
    # Distinct approver decides; a token is minted.
    store.decide(pending[0].id, True, decided_by="human")
    token = store.issue(pending[0].id, decided_by="human", max_uses=1)
    result = instance.act(req("launch", target="notepad",
                              reason="open editor"),
                          approval_token_id=token.id)
    assert result.decision == "ALLOW"
    assert result.executed
    assert result.observation["app"] == "notepad"


def test_token_is_single_use_and_scope_bound():
    store = ApprovalStore()
    instance = agent(store=store)
    instance.act(req("launch", target="notepad"))
    pending = store.pending()[0]
    store.decide(pending.id, True, decided_by="human")
    token = store.issue(pending.id, decided_by="human", max_uses=1)
    first = instance.act(req("launch", target="notepad"),
                         approval_token_id=token.id)
    assert first.executed
    # Replay: exhausted token fails closed and files a NEW approval.
    replay = instance.act(req("launch", target="notepad"),
                          approval_token_id=token.id)
    assert not replay.executed
    assert replay.approval_required
    # Different target: token does not cover it.
    instance.act(req("launch", target="notepad"))
    new_request = store.pending()[-1]
    store.decide(new_request.id, True, decided_by="human")
    other = store.issue(new_request.id, decided_by="human", max_uses=1)
    out_of_scope = instance.act(req("launch", target="calculator"),
                                approval_token_id=other.id)
    assert not out_of_scope.executed


def test_task_scope_grants_enforced():
    checker = GrantScopeChecker()
    instance = agent(scope_checker=checker,
                     profile=DesktopProfile(mode="autonomous"))
    # No grant: task-bound actuation denied.
    result = instance.act(req("mouse_move", params={"x": 1, "y": 2},
                              task_id="t1"))
    assert result.decision == "DENY"
    assert any("no desktop grant" in reason
               for reason in result.to_dict()["reasons"])
    # Grant covers input; low-risk actuation runs automatically.
    checker.grant("t1", ("input",))
    result = instance.act(req("mouse_move", params={"x": 1, "y": 2},
                              task_id="t1"))
    assert result.decision == "ALLOW" and result.executed
    # Grant does not cover launching apps.
    result = instance.act(req("launch", target="notepad", task_id="t1"))
    assert result.decision == "DENY"
    # Interactive requests (no task) pass the scope layer.
    result = instance.act(req("mouse_move", params={"x": 1, "y": 2}))
    assert result.decision == "ALLOW" and result.executed


def test_grant_expiry_and_revocation():
    import time

    clock = [1000.0]
    checker = GrantScopeChecker(ttl=60.0, clock=lambda: clock[0])
    checker.grant("t1", ("input",))
    assert checker.active("t1") == ("input",)
    clock[0] = 1061.0
    assert checker.active("t1") is None
    checker.grant("t2", ("screen",), ttl=600)
    assert checker.revoke("t2")
    assert not checker.revoke("t2")
    assert checker.active("t2") is None


def test_profile_safe_blocks_actuation_even_when_policy_allows():
    instance = agent(profile=DesktopProfile(mode="safe"))
    result = instance.act(req("keyboard", params={"text": "hi"}))
    assert result.decision == "DENY"
    assert any("profile" in reason
               for reason in result.to_dict()["reasons"])


def test_provider_failure_is_structured_and_recoverable():
    provider = FakeDesktopProvider()
    provider.disconnect()
    instance = agent(provider=provider)
    result = instance.observe(req("screenshot"))
    assert result.error == {"kind": "disconnected",
                            "message": "desktop provider is disconnected"}
    assert not result.executed
    assert result.recoverable
    provider.reconnect()
    assert instance.observe(req("screenshot")).executed


def test_audit_trail_covers_evaluations_and_executions():
    audit = AuditLog()
    store = ApprovalStore()
    instance = agent(store=store, audit=audit)
    instance.scope_checker.grant("t9", ("*",))
    instance.observe(req("screenshot", task_id="t9"))
    instance.act(req("launch", target="notepad", task_id="t9"))
    instance.act(req("launch", target="bitwarden", task_id="t9"))
    recorded = [event.to_dict() for event in audit.events]
    assert any(event["operation"] == "screenshot" for event in recorded)
    assert any(event["operation"] == "launch" and
               event["decision"] == "DENY" and "credential"
               in event["reason"] for event in recorded)
    assert len(audit) >= 3


def test_authorize_never_executes():
    provider = FakeDesktopProvider()
    instance = agent(provider=provider)
    authorization = instance.authorize(req("launch", target="notepad"))
    assert authorization.decision.value == "REQUIRE_APPROVAL"
    assert provider.snapshot()["window_count"] == 1  # untouched
    authorization = instance.authorize(req("screenshot"))
    assert authorization.allowed
    assert authorization.risk == "NONE"
    assert authorization.to_dict()["profile_verdict"] == "AUTO"


def test_capabilities_reflect_profile_and_provider_health():
    provider = FakeDesktopProvider()
    instance = agent(provider=provider)
    capabilities = instance.available_actions()
    assert len(capabilities) == 15
    assert all(item["executable"] for item in capabilities)
    by_action = {item["action"]: item for item in capabilities}
    assert by_action["screenshot"]["observation"] is True
    assert by_action["launch"]["observation"] is False
    provider.disconnect()
    capabilities = instance.available_actions()
    assert all(not item["executable"] for item in capabilities)
