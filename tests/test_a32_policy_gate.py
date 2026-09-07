"""Explicit permission/policy gate tests (A32.2 rebuild).

Every change set passes ALLOW / DENY / REQUIRE_APPROVAL evaluation before
modification, with the operation, path, tool, risk, and requested capability
visible to the decision. Denials are never bypassed by approval.
"""
import pytest

from forge.runtime.defaults import create_default_runtime
from forge.security.permissions import OperationMode, PermissionManager
from forge.security.policy_gate import PolicyDecision, PolicyGate
from forge.tools.change_applier import ChangeApplier, CodeChange
from forge.tools.checkpoint import CheckpointManager


def _gate(mode=OperationMode.ASSISTED):
    return PolicyGate(PermissionManager(mode=mode))


def _applier(root, mode=OperationMode.ASSISTED):
    runtime = create_default_runtime(PermissionManager(mode=mode), str(root))
    return ChangeApplier(runtime, CheckpointManager(root), root=str(root))


# -- decisions ------------------------------------------------------------

def test_assisted_requires_approval_for_writes():
    gate = _gate()
    denied = gate.evaluate(operation="write_file", path="app.py",
                           tool="write_file", approved=False)
    assert denied.decision == PolicyDecision.REQUIRE_APPROVAL
    assert not denied.allowed
    allowed = gate.evaluate(operation="write_file", path="app.py",
                            tool="write_file", approved=True)
    assert allowed.decision == PolicyDecision.ALLOW
    assert allowed.allowed


def test_safe_mode_denies_writes_even_when_approved():
    outcome = _gate(OperationMode.SAFE).evaluate(
        operation="write_file", path="app.py", tool="write_file", approved=True)
    assert outcome.decision == PolicyDecision.DENY
    assert not outcome.allowed


def test_safe_mode_still_allows_reads():
    outcome = _gate(OperationMode.SAFE).evaluate(
        operation="read_file", path="app.py", tool="read_file")
    assert outcome.decision == PolicyDecision.ALLOW
    assert outcome.allowed


def test_locked_mode_denies_modifications():
    outcome = _gate(OperationMode.LOCKED).evaluate(
        operation="write_file", path="app.py", tool="write_file", approved=True)
    assert outcome.decision == PolicyDecision.DENY
    assert not outcome.allowed


def test_blocked_operations_deny_in_every_mode_even_when_approved():
    for mode in OperationMode:
        for operation in ("delete_repository", "expose_secrets"):
            outcome = _gate(mode).evaluate(operation=operation, approved=True)
            assert outcome.decision == PolicyDecision.DENY, (mode, operation)
            assert not outcome.allowed


def test_protected_paths_deny_writes_and_deletes():
    gate = _gate(OperationMode.AUTONOMOUS)
    for path in (".git/config", ".forge/state.json", ".env",
                 "credentials.json", "../outside.py"):
        write = gate.evaluate(operation="write_file", path=path,
                              tool="write_file", approved=True)
        assert write.decision == PolicyDecision.DENY, path
        delete = gate.evaluate(operation="delete_file", path=path,
                               tool="delete_file", approved=True)
        assert delete.decision == PolicyDecision.DENY, path


def test_autonomous_auto_approves_only_low_risk_writes():
    gate = _gate(OperationMode.AUTONOMOUS)
    low = gate.evaluate(operation="write_file", path="app.py",
                        tool="write_file", risk="LOW", approved=False)
    assert low.decision == PolicyDecision.ALLOW
    assert low.allowed
    high = gate.evaluate(operation="write_file", path="app.py",
                         tool="write_file", risk="HIGH", approved=False)
    assert high.decision == PolicyDecision.REQUIRE_APPROVAL
    assert not high.allowed
    approved_high = gate.evaluate(
        operation="write_file", path="app.py", tool="write_file",
        risk="CRITICAL", approved=True)
    assert approved_high.decision == PolicyDecision.ALLOW
    assert approved_high.allowed


def test_autonomous_still_requires_approval_for_sensitive_ops():
    gate = _gate(OperationMode.AUTONOMOUS)
    for operation in ("delete_file", "run_command", "git_commit", "git_push"):
        outcome = gate.evaluate(operation=operation, approved=False)
        assert outcome.decision == PolicyDecision.REQUIRE_APPROVAL, operation
        assert not outcome.allowed


def test_unknown_risk_fails_closed_in_autonomous():
    outcome = _gate(OperationMode.AUTONOMOUS).evaluate(
        operation="write_file", path="app.py", tool="write_file",
        risk="bogus", approved=False)
    assert outcome.risk == "HIGH"
    assert outcome.decision == PolicyDecision.REQUIRE_APPROVAL
    assert not outcome.allowed


def test_unknown_operation_requires_approval():
    outcome = _gate().evaluate(operation="launch_missiles", approved=False)
    assert outcome.decision == PolicyDecision.REQUIRE_APPROVAL
    assert not outcome.allowed


def test_decision_records_full_request_context():
    outcome = _gate().evaluate(
        operation="write_file", path="app.py", tool="write_file",
        risk="MEDIUM", capability="coding", approved=True)
    assert outcome.to_dict() == {
        "decision": "ALLOW", "allowed": True,
        "reason": "Explicit approval granted", "operation": "write_file",
        "path": "app.py", "tool": "write_file", "risk": "MEDIUM",
        "capability": "coding",
    }


# -- engine integration ---------------------------------------------------

def test_apply_blocks_unapproved_write_before_modification(tmp_path):
    (tmp_path / "app.py").write_text("original\n")
    result = _applier(tmp_path).apply(
        [CodeChange(path="app.py", content="changed\n")],
        approved=False, capability="coding")
    assert not result.success
    assert result.error_details[0].code == "APPROVAL_REQUIRED"
    assert result.decisions[0]["decision"] == "REQUIRE_APPROVAL"
    assert result.decisions[0]["capability"] == "coding"
    assert (tmp_path / "app.py").read_text() == "original\n"


def test_apply_denial_cannot_be_bypassed_with_approval(tmp_path):
    (tmp_path / "app.py").write_text("original\n")
    result = _applier(tmp_path, mode=OperationMode.SAFE).apply(
        [CodeChange(path="app.py", content="changed\n")], approved=True)
    assert not result.success
    assert result.error_details[0].code == "POLICY_DENIED"
    assert result.decisions[0]["decision"] == "DENY"
    assert (tmp_path / "app.py").read_text() == "original\n"


def test_apply_records_allow_decisions_on_success(tmp_path):
    result = _applier(tmp_path).apply(
        [CodeChange(path="app.py", content="x = 1\n")],
        approved=True, capability="coding")
    assert result.success
    assert result.decisions[0]["decision"] == "ALLOW"
    assert result.decisions[0]["operation"] == "write_file"
    assert result.decisions[0]["tool"] == "write_file"


def test_apply_denies_high_risk_write_in_autonomous(tmp_path):
    (tmp_path / "app.py").write_text("original\n")
    result = _applier(tmp_path, mode=OperationMode.AUTONOMOUS).apply(
        [CodeChange(path="app.py", content="changed\n", risk="HIGH")],
        approved=False)
    assert not result.success
    assert result.error_details[0].code == "APPROVAL_REQUIRED"
    assert (tmp_path / "app.py").read_text() == "original\n"


def test_dry_run_previews_decisions_without_filtering(tmp_path):
    applier = _applier(tmp_path)
    result = applier.dry_run(
        [CodeChange(path="app.py", content="x = 1\n")], capability="coding")
    assert result.valid
    assert result.would_change == ["app.py"]
    assert result.decisions[0]["decision"] == "REQUIRE_APPROVAL"
    assert result.decisions[0]["capability"] == "coding"
    assert not (tmp_path / "app.py").exists()


@pytest.mark.parametrize("mode", list(OperationMode))
def test_delete_decision_requires_approval_in_every_mode(tmp_path, mode):
    gate = _gate(mode)
    without = gate.evaluate(operation="delete_file", path="app.py",
                            tool="delete_file", approved=False)
    assert not without.allowed
