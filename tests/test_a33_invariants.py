"""A33 security invariants 1-10 (explicit, tested, non-negotiable)."""
import pytest

from forge.runtime.defaults import create_default_runtime
from forge.runtime.runtime import ToolDefinition, ToolResult
from forge.security.approvals import ApprovalRequest, ApprovalStore
from forge.security.audit import AuditLog
from forge.security.permissions import OperationMode, PermissionManager
from forge.security.policy import (
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
    Resource,
)
from forge.security.policy_gate import PolicyDecision, PolicyGate
from forge.tools.change_applier import ChangeApplier, CodeChange


def _rule(rule_id, resource, operation, effect, scope="", **kwargs):
    return PermissionRule(rule_id, resource, operation, effect, scope,
                          reason="invariant", **kwargs)


def _manager(mode=OperationMode.ASSISTED, policy=None, store=None, audit=None):
    return PermissionManager(mode=mode, policy=policy, store=store,
                             agent="test", audit=audit)


def _token(store, agent="coder", task_id="t", **kwargs):
    values = {"agent": agent, "resource": Resource.FILESYSTEM,
              "operation": "write", "scopes": ("src/a.py",), "task_id": task_id,
              "reason": "x"}
    values.update(kwargs)
    request = store.submit(ApprovalRequest(**values))
    store.decide(request.id, True, "operator")
    return store.issue(request.id, "operator")


# Invariant 1: DENY can never become ALLOW through agent logic.
def test_invariant_1_deny_never_becomes_allow(tmp_path):
    policy = PermissionPolicy(rules=[
        _rule("broad-allow", Resource.FILESYSTEM, "write", "ALLOW", "**")])
    gate = PolicyGate(_manager(OperationMode.SAFE, policy))
    outcome = gate.evaluate(operation="write_file", path="a.py",
                            approved=True)
    assert outcome.decision == PolicyDecision.DENY
    runtime = create_default_runtime(
        _manager(OperationMode.SAFE, policy), str(tmp_path))
    assert not runtime.execute("write_file", approved=True, path="a.py",
                               content="x").success


# Invariant 2: REQUIRE_APPROVAL cannot execute without approval.
def test_invariant_2_no_approval_no_execution(tmp_path):
    manager = _manager()
    runtime = create_default_runtime(manager, str(tmp_path))
    assert not runtime.execute("write_file", approved=False, path="a.py",
                               content="x").success
    applier = ChangeApplier(runtime, root=str(tmp_path))
    result = applier.apply([CodeChange(path="b.py", content="x = 1\n")],
                           approved=False)
    assert not result.success
    assert not (tmp_path / "a.py").exists()
    assert not (tmp_path / "b.py").exists()


# Invariant 3: an approval cannot expand its original scope.
def test_invariant_3_approval_cannot_expand_scope():
    store = ApprovalStore()
    token = _token(store)
    gate = PolicyGate(_manager(store=store))
    other_file = gate.evaluate(
        operation="write_file", path="src/other.py", approved=False,
        agent="coder", task_id="t", approval_token_id=token.id)
    assert other_file.decision == PolicyDecision.REQUIRE_APPROVAL
    other_op = gate.evaluate(
        operation="delete_file", path="src/a.py", approved=False,
        agent="coder", task_id="t", approval_token_id=token.id)
    assert other_op.decision == PolicyDecision.REQUIRE_APPROVAL
    other_task = gate.evaluate(
        operation="write_file", path="src/a.py", approved=False,
        agent="coder", task_id="elsewhere", approval_token_id=token.id)
    assert other_task.decision == PolicyDecision.REQUIRE_APPROVAL
    other_agent = gate.evaluate(
        operation="write_file", path="src/a.py", approved=False,
        agent="intruder", task_id="t", approval_token_id=token.id)
    assert other_agent.decision == PolicyDecision.REQUIRE_APPROVAL


# Invariant 4: an expired approval cannot execute.
def test_invariant_4_expired_approval_cannot_execute():
    box = {"t": 1000.0}
    store = ApprovalStore(clock=lambda: box["t"])
    token = _token(store)
    box["t"] += 100000.0
    gate = PolicyGate(_manager(store=store))
    outcome = gate.evaluate(
        operation="write_file", path="src/a.py", approved=False,
        agent="coder", task_id="t", approval_token_id=token.id)
    assert outcome.decision == PolicyDecision.REQUIRE_APPROVAL
    grant = store.grant_task("t", ["a.py"], "fp", ttl_seconds=10)
    assert grant.expired(box["t"] + 100000.0)
    assert store.check_task_grant("t", "a.py", "fp",
                                  now=box["t"] + 100000.0)[0] is False


# Invariant 5: an agent cannot grant itself permission.
def test_invariant_5_no_self_grant():
    store = ApprovalStore()
    request = store.submit(ApprovalRequest(
        agent="coder", resource=Resource.FILESYSTEM, operation="write",
        scopes=("a.py",), reason="x"))
    with pytest.raises(ValueError, match="cannot approve its own"):
        store.decide(request.id, True, "coder")
    store.decide(request.id, True, "operator")
    with pytest.raises(ValueError, match="recorded approver"):
        store.issue(request.id, "coder")


# Invariant 6: a broad ALLOW cannot override a more-specific DENY.
def test_invariant_6_specific_deny_beats_broad_allow(tmp_path):
    policy = PermissionPolicy(rules=[
        _rule("broad", Resource.FILESYSTEM, "write", "ALLOW", "**"),
        _rule("specific", Resource.FILESYSTEM, "write", "DENY",
              "src/secrets/**"),
    ])
    gate = PolicyGate(_manager(OperationMode.AUTONOMOUS, policy))
    denied = gate.evaluate(operation="write_file", path="src/secrets/k",
                           approved=True)
    assert denied.decision == PolicyDecision.DENY
    allowed = gate.evaluate(operation="write_file", path="src/a.py",
                            approved=True)
    assert allowed.decision == PolicyDecision.ALLOW


# Invariant 7: unknown permissions fail closed.
def test_invariant_7_unknown_fails_closed(tmp_path):
    policy = PermissionPolicy()
    assert policy.evaluate(PermissionRequest(
        agent="a", resource=Resource.BROWSER, operation="navigate",
        scope="https://example.com/")).decision == PolicyDecision.DENY
    runtime = create_default_runtime(_manager(), str(tmp_path))
    assert not runtime.execute("no_such_tool", approved=True).success


# Invariant 8: protected resources remain protected.
def test_invariant_8_protected_paths_deny_everything(tmp_path):
    store = ApprovalStore()
    policy = PermissionPolicy(rules=[
        _rule("open", Resource.FILESYSTEM, "write", "ALLOW", "**")])
    gate = PolicyGate(_manager(OperationMode.AUTONOMOUS, policy, store))
    token = _token(store, scopes=("**",))
    for path in (".git/config", ".forge/state.json", ".env", "db_secret.json"):
        outcome = gate.evaluate(
            operation="write_file", path=path, approved=True,
            agent="coder", task_id="t", approval_token_id=token.id)
        assert outcome.decision == PolicyDecision.DENY, path


# Invariant 9: permission checks happen before side effects.
def test_invariant_9_checks_precede_side_effects(tmp_path):
    calls = []

    def handler(path):
        calls.append(path)
        return ToolResult.ok("probe")

    manager = _manager()
    runtime = create_default_runtime(manager, str(tmp_path))
    runtime.register(ToolDefinition(
        name="probe", description="probe", handler=handler,
        permission="write_file"))
    assert not runtime.execute("probe", approved=False, path="x").success
    assert calls == []
    assert runtime.execute("probe", approved=True, path="x").success
    assert calls == ["x"]


# Invariant 10: every decision is auditable without exposing secrets.
def test_invariant_10_decisions_audited_secret_free(tmp_path):
    audit = AuditLog()
    secret = "AKIAIOSFODNN7EXAMPLE"
    runtime = create_default_runtime(
        _manager(audit=audit), str(tmp_path))
    runtime.execute("write_file", approved=False, path="a.py", content="x")
    runtime.execute("write_file", approved=True, path="a.py", content="x")
    audit.record_decision(agent="a", resource="filesystem", operation="write",
                          decision="DENY", reason=f"blocked {secret}")
    decisions = {event.decision for event in audit.events}
    assert PolicyDecision.REQUIRE_APPROVAL in decisions
    assert PolicyDecision.ALLOW in decisions
    assert PolicyDecision.DENY in decisions
    import json as _json
    assert secret not in _json.dumps(audit.to_dict())
