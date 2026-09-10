"""Atomic ChangeSet transaction-boundary tests (PR #5 final hardening).

Invariant under test:

    INVALID CHANGESET      -> NO FILE WRITES
    UNAUTHORIZED CHANGESET -> NO FILE WRITES
    VALID + AUTHORIZED     -> CHECKPOINT -> APPLY
"""
from __future__ import annotations
import hashlib

import pytest

from forge.runtime.defaults import create_default_runtime
from forge.runtime.runtime import ToolResult
from forge.security.approvals import ApprovalRequest, ApprovalStore
from forge.security.permissions import OperationMode, PermissionManager
from forge.security.policy import PermissionPolicy, PermissionRule, Resource
from forge.tools.change_applier import ChangeApplier, CodeChange
from forge.tools.checkpoint import CheckpointManager


def _applier(root, mode=OperationMode.ASSISTED, with_checkpoint=True,
             policy=None):
    runtime = create_default_runtime(
        PermissionManager(mode=mode, policy=policy), str(root))
    manager = CheckpointManager(root) if with_checkpoint else None
    return ChangeApplier(runtime, manager, root=str(root))


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- Test A: later validation failure --------------------------------------

@pytest.mark.parametrize("with_checkpoint", [True, False])
def test_later_validation_failure_writes_nothing(tmp_path, with_checkpoint):
    (tmp_path / "a.py").write_text("a-original\n")
    (tmp_path / "b.py").write_text("b-original\n")
    applier = _applier(tmp_path, with_checkpoint=with_checkpoint)

    result = applier.apply([
        CodeChange(path="a.py", content="a-changed\n"),
        CodeChange(path="b.py", content="b-changed\n"),
        CodeChange(path="../escape.py", content="x = 1\n"),
    ], approved=True)

    assert not result.success
    assert result.changed_paths == []
    assert result.checkpoint_id is None
    assert result.rolled_back is False  # nothing to roll back: no writes
    assert (tmp_path / "a.py").read_text() == "a-original\n"
    assert (tmp_path / "b.py").read_text() == "b-original\n"
    assert not (tmp_path.parent / "escape.py").exists()


# -- Test B: later policy denial -------------------------------------------

def test_later_policy_denial_writes_nothing(tmp_path):
    (tmp_path / "a.py").write_text("a-original\n")
    (tmp_path / "b.py").write_text("b-original\n")
    policy = PermissionPolicy(rules=[
        PermissionRule("deny-blocked", Resource.FILESYSTEM, "write", "DENY",
                       scope="blocked/**", reason="transaction test"),
    ])
    applier = _applier(tmp_path, mode=OperationMode.AUTONOMOUS, policy=policy)

    result = applier.apply([
        CodeChange(path="a.py", content="a-changed\n", risk="LOW"),
        CodeChange(path="b.py", content="b-changed\n", risk="LOW"),
        CodeChange(path="blocked/c.py", content="x = 1\n", risk="LOW"),
    ], approved=True, capability="coding")

    assert not result.success
    assert result.error_details[-1].code == "POLICY_DENIED"
    assert result.changed_paths == []
    assert result.checkpoint_id is None
    assert (tmp_path / "a.py").read_text() == "a-original\n"
    assert (tmp_path / "b.py").read_text() == "b-original\n"
    assert not (tmp_path / "blocked").exists()


# -- Test C: approval required ---------------------------------------------

def test_approval_required_change_blocks_whole_transaction(tmp_path):
    (tmp_path / "a.py").write_text("a-original\n")
    (tmp_path / "b.py").write_text("b-original\n")
    applier = _applier(tmp_path, mode=OperationMode.AUTONOMOUS)

    result = applier.apply([
        CodeChange(path="a.py", content="a-changed\n", risk="LOW"),
        CodeChange(path="b.py", content="b-changed\n", risk="HIGH"),
    ], approved=False, capability="coding")

    assert not result.success
    assert any(detail.code == "APPROVAL_REQUIRED"
               for detail in result.error_details)
    assert result.changed_paths == []
    assert result.checkpoint_id is None
    assert (tmp_path / "a.py").read_text() == "a-original\n"
    assert (tmp_path / "b.py").read_text() == "b-original\n"


def test_token_exhausted_mid_transaction_fails_closed(tmp_path):
    (tmp_path / "a.py").write_text("a-original\n")
    store = ApprovalStore()
    request = store.submit(ApprovalRequest(
        agent="coder", resource=Resource.FILESYSTEM, operation="write",
        scopes=("a.py", "b.py"), task_id="t", reason="transaction test"))
    store.decide(request.id, True, "operator")
    token = store.issue(request.id, "operator")  # single use
    runtime = create_default_runtime(
        PermissionManager(mode=OperationMode.ASSISTED, store=store),
        str(tmp_path))
    applier = ChangeApplier(runtime, CheckpointManager(tmp_path),
                            root=str(tmp_path), approval_store=store)

    result = applier.apply([
        CodeChange(path="a.py", content="a-changed\n"),
        CodeChange(path="b.py", content="b = 1\n"),
    ], approved=False, actor="coder", task_id="t",
        approval_token_id=token.id)

    # The single use authorizes the first execution; the second fails
    # closed and the transaction rolls back to zero candidate writes.
    assert not result.success
    assert result.rolled_back is True
    assert (tmp_path / "a.py").read_text() == "a-original\n"
    assert not (tmp_path / "b.py").exists()


# -- Test D: valid complete transaction ------------------------------------

def test_valid_transaction_applies_everything(tmp_path):
    applier = _applier(tmp_path)
    result = applier.apply([
        CodeChange(path="a.py", content="a = 1\n"),
        CodeChange(path="b.py", content="b = 2\n"),
        CodeChange(path="c.py", content="c = 3\n"),
    ], approved=True, capability="coding")

    assert result.success
    assert result.changed_paths == ["a.py", "b.py", "c.py"]
    assert result.checkpoint_id
    assert result.rolled_back is False
    assert [item["decision"] for item in result.decisions] == ["ALLOW"] * 3
    assert (tmp_path / "a.py").read_text() == "a = 1\n"
    assert (tmp_path / "b.py").read_text() == "b = 2\n"
    assert (tmp_path / "c.py").read_text() == "c = 3\n"
    applier.checkpoint_manager.cleanup(result.checkpoint)


# -- Test E: mid-application filesystem failure ----------------------------

@pytest.mark.parametrize("failure", ["failed-result", "raised-exception"])
def test_mid_application_failure_rolls_back(tmp_path, monkeypatch, failure):
    (tmp_path / "a.py").write_text("a-original\n")
    (tmp_path / "unrelated.txt").write_text("user work\n")
    applier = _applier(tmp_path)
    real_execute = applier.runtime.execute

    def flaky(tool_name, **kwargs):
        if kwargs.get("path") == "b.py":
            if failure == "raised-exception":
                raise OSError("simulated disk failure")
            return ToolResult.fail(tool_name, "simulated disk failure")
        return real_execute(tool_name, **kwargs)

    monkeypatch.setattr(applier.runtime, "execute", flaky)
    result = applier.apply([
        CodeChange(path="a.py", content="a-changed\n"),
        CodeChange(path="b.py", content="b = 1\n"),
        CodeChange(path="c.py", content="c = 1\n"),
    ], approved=True)

    assert not result.success
    assert result.error_details[0].code == "WRITE_FAILED"
    assert result.rolled_back is True
    assert result.checkpoint_id is None
    # Candidate files restored; later files never attempted.
    assert (tmp_path / "a.py").read_text() == "a-original\n"
    assert not (tmp_path / "b.py").exists()
    assert not (tmp_path / "c.py").exists()
    # Unrelated user work preserved.
    assert (tmp_path / "unrelated.txt").read_text() == "user work\n"


# -- Test F: conflicting changes -------------------------------------------

def test_conflicting_modify_and_delete_rejected(tmp_path):
    (tmp_path / "app.py").write_text("original\n")
    applier = _applier(tmp_path)
    result = applier.apply([
        CodeChange(path="app.py", content="changed\n", action="modify"),
        CodeChange(path="app.py", content="", action="delete"),
    ], approved=True, allow_delete=True)

    assert not result.success
    assert any(detail.code == "CONFLICTING_CHANGES"
               for detail in result.error_details)
    assert result.changed_paths == []
    assert result.checkpoint_id is None
    assert (tmp_path / "app.py").read_text() == "original\n"


def test_conflicting_contents_for_same_path_rejected(tmp_path):
    applier = _applier(tmp_path)
    result = applier.apply([
        CodeChange(path="app.py", content="one = 1\n"),
        CodeChange(path="app.py", content="two = 2\n"),
    ], approved=True)

    assert not result.success
    assert any(detail.code == "CONFLICTING_CHANGES"
               for detail in result.error_details)
    assert not (tmp_path / "app.py").exists()


def test_identical_duplicate_entries_apply_once(tmp_path):
    applier = _applier(tmp_path)
    change = CodeChange(path="app.py", content="x = 1\n")
    result = applier.apply([change, change], approved=True)

    assert result.success
    assert result.changed_paths == ["app.py"]
    assert (tmp_path / "app.py").read_text() == "x = 1\n"
    applier.checkpoint_manager.cleanup(result.checkpoint)


# -- Test G: hash/content guard --------------------------------------------

def test_stale_hash_guard_rejects_before_any_write(tmp_path):
    (tmp_path / "a.py").write_text("a-original\n")
    (tmp_path / "b.py").write_text("b-original\n")
    applier = _applier(tmp_path)
    result = applier.apply([
        CodeChange(path="a.py", content="a-changed\n"),
        CodeChange(path="b.py", content="b-changed\n",
                   expected_old_hash=_sha("stale\n")),
    ], approved=True)

    assert not result.success
    assert result.error_details[0].code == "OLD_STATE_MISMATCH"
    assert result.changed_paths == []
    assert result.checkpoint_id is None
    assert (tmp_path / "a.py").read_text() == "a-original\n"
    assert (tmp_path / "b.py").read_text() == "b-original\n"


# -- Test H: security regression -------------------------------------------

@pytest.mark.parametrize("attack", [
    CodeChange(path="../escape.py", content="x = 1\n"),
    CodeChange(path="src/.git/config", content="x\n"),
    CodeChange(path="nested/.forge/state.json", content="{}\n"),
    CodeChange(path=".env", content="X=1\n"),
    CodeChange(path="credentials.json", content="{}\n"),
    CodeChange(path="app.py", content="api_key = 'sup3r-secret-value'\n"),
    CodeChange(path="app.py", content="-----BEGIN RSA PRIVATE KEY-----\n"),
    CodeChange(path="app.py", content="def broken(:\n"),
])
def test_transaction_hardening_keeps_security_protections(tmp_path, attack):
    (tmp_path / "ok.py").write_text("ok-original\n")
    applier = _applier(tmp_path)
    result = applier.apply([
        CodeChange(path="ok.py", content="ok-changed\n"),
        attack,
    ], approved=True)

    assert not result.success
    assert result.changed_paths == []
    assert result.checkpoint_id is None
    assert (tmp_path / "ok.py").read_text() == "ok-original\n"


def test_transaction_hardening_keeps_mode_and_approval_denials(tmp_path):
    (tmp_path / "ok.py").write_text("ok-original\n")
    safe = _applier(tmp_path, mode=OperationMode.SAFE)
    denied = safe.apply([
        CodeChange(path="ok.py", content="ok-changed\n"),
        CodeChange(path="other.py", content="x = 1\n"),
    ], approved=True)
    assert not denied.success
    assert any(detail.code == "POLICY_DENIED"
               for detail in denied.error_details)
    assert (tmp_path / "ok.py").read_text() == "ok-original\n"

    assisted = _applier(tmp_path, mode=OperationMode.ASSISTED)
    waiting = assisted.apply([
        CodeChange(path="ok.py", content="ok-changed\n"),
        CodeChange(path="other.py", content="x = 1\n"),
    ], approved=False)
    assert not waiting.success
    assert any(detail.code == "APPROVAL_REQUIRED"
               for detail in waiting.error_details)
    assert (tmp_path / "ok.py").read_text() == "ok-original\n"
    assert not (tmp_path / "other.py").exists()


# -- ordering + observability ----------------------------------------------

def test_checkpoint_created_after_validation_and_before_writes(
        tmp_path, monkeypatch):
    applier = _applier(tmp_path)
    events: list[str] = []
    real_create = applier.checkpoint_manager.create
    real_execute = applier.runtime.execute

    def spy_create(label):
        events.append("checkpoint")
        return real_create(label)

    def spy_execute(tool_name, **kwargs):
        events.append(f"write:{kwargs.get('path')}")
        return real_execute(tool_name, **kwargs)

    monkeypatch.setattr(applier.checkpoint_manager, "create", spy_create)
    monkeypatch.setattr(applier.runtime, "execute", spy_execute)

    bad = applier.apply([
        CodeChange(path="a.py", content="x = 1\n"),
        CodeChange(path="../escape.py", content="x = 1\n"),
    ], approved=True)
    assert not bad.success
    assert events == []  # no checkpoint, no writes on pre-flight failure

    good = applier.apply([
        CodeChange(path="a.py", content="x = 1\n"),
        CodeChange(path="b.py", content="y = 2\n"),
    ], approved=True)
    assert good.success
    assert events == ["checkpoint", "write:a.py", "write:b.py"]
    applier.checkpoint_manager.cleanup(good.checkpoint)


def test_malformed_entries_rejected_without_writes(tmp_path):
    applier = _applier(tmp_path)
    result = applier.apply([
        {"path": "a.py", "content": "x = 1\n"},
        {"content": "missing path\n"},
    ], approved=True)

    assert not result.success
    assert result.error_details[0].code == "MALFORMED_CHANGESET"
    assert result.changed_paths == []
    assert result.checkpoint_id is None
    assert not (tmp_path / "a.py").exists()


def test_transaction_observability_without_secrets(tmp_path):
    (tmp_path / "a.py").write_text("a-original\n")
    applier = _applier(tmp_path)
    result = applier.apply([
        CodeChange(path="a.py", content="a-changed\n"),
        CodeChange(path="b.py", content="token = 'sup3r-secret-value'\n"),
    ], approved=True)

    assert not result.success
    assert result.fingerprint
    assert result.proposed_paths == ["a.py", "b.py"]
    assert result.duration_ms is not None and result.duration_ms >= 0
    assert result.rolled_back is False
    assert (tmp_path / "a.py").read_text() == "a-original\n"
    payload = repr(result.to_dict())
    assert "sup3r-secret-value" not in payload
    assert any(detail.code == "SECRET_CONTENT"
               for detail in result.error_details)
