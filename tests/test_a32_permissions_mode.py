"""Permission operation-mode tests (A32.17)."""

from forge.runtime.defaults import create_default_runtime
from forge.security.permissions import OperationMode, PermissionManager


def _runtime(root, mode):
    return create_default_runtime(PermissionManager(mode=mode), str(root))


def test_default_mode_is_assisted_and_requires_approval(tmp_path):
    manager = PermissionManager()
    assert manager.mode == OperationMode.ASSISTED
    allowed, reason = manager.may_execute("write_file", approved=False)
    assert not allowed and "Approval" in reason
    assert manager.may_execute("write_file", approved=True)[0]


def test_autonomous_auto_approves_writes_only(tmp_path):
    manager = PermissionManager(mode=OperationMode.AUTONOMOUS)
    assert manager.may_execute("write_file", approved=False)[0]
    # Destructive / sensitive operations still require explicit approval.
    assert not manager.may_execute("run_command", approved=False)[0]
    assert not manager.may_execute("delete_file", approved=False)[0]
    assert not manager.may_execute("git_commit", approved=False)[0]
    assert not manager.may_execute("git_push", approved=False)[0]
    # Explicitly approved sensitive ops are allowed.
    assert manager.may_execute("run_command", approved=True)[0]


def test_safe_mode_blocks_modifications(tmp_path):
    manager = PermissionManager(mode=OperationMode.SAFE)
    assert manager.may_execute("read_file", approved=False)[0]
    assert manager.may_execute("git_status", approved=False)[0]
    assert not manager.may_execute("write_file", approved=True)[0]
    assert not manager.may_execute("run_command", approved=True)[0]


def test_locked_mode_blocks_everything_but_reads(tmp_path):
    manager = PermissionManager(mode=OperationMode.LOCKED)
    assert manager.may_execute("read_file", approved=False)[0]
    assert not manager.may_execute("write_file", approved=True)[0]
    assert not manager.may_execute("run_command", approved=True)[0]


def test_blocked_operations_never_escalate(tmp_path):
    for mode in OperationMode:
        manager = PermissionManager(mode=mode)
        assert not manager.may_execute("delete_repository", approved=True)[0]
        assert not manager.may_execute("expose_secrets", approved=True)[0]


def test_runtime_enforces_autonomous_mode(tmp_path):
    runtime = _runtime(tmp_path, OperationMode.AUTONOMOUS)
    write = runtime.execute("write_file", approved=False, path="app.py", content="x = 1\n")
    assert write.success
    assert (tmp_path / "app.py").exists()


def test_runtime_enforces_locked_mode(tmp_path):
    runtime = _runtime(tmp_path, OperationMode.LOCKED)
    write = runtime.execute("write_file", approved=True, path="app.py", content="x = 1\n")
    assert not write.success
    assert "LOCKED" in write.error
    assert not (tmp_path / "app.py").exists()
