"""Controlled code-change application layer tests (A32.4)."""
import pytest

from forge.runtime.defaults import create_default_runtime
from forge.security.permissions import OperationMode, PermissionManager
from forge.tools.change_applier import ChangeApplier, CodeChange
from forge.tools.checkpoint import CheckpointManager


def _applier(root, mode=OperationMode.ASSISTED, with_checkpoint=True):
    runtime = create_default_runtime(PermissionManager(mode=mode), str(root))
    manager = CheckpointManager(root) if with_checkpoint else None
    return ChangeApplier(runtime, manager)


# -- validation ------------------------------------------------------------

@pytest.mark.parametrize("bad_path", [
    "/etc/passwd",
    "../outside.py",
    "a/../../b.py",
    "src/../escape.py",
    ".git/config",
    ".forge/state.json",
    "dir\\win.py",
    "",
])
def test_applier_rejects_unsafe_paths(tmp_path, bad_path):
    applier = _applier(tmp_path)
    with pytest.raises(ValueError):
        applier.validate(CodeChange(path=bad_path, content="x = 1\n"))


def test_applier_rejects_secret_and_credential_files(tmp_path):
    applier = _applier(tmp_path)
    with pytest.raises(ValueError):
        applier.validate(CodeChange(path="app.py", content="api_key = 'sup3r-secret-value'\n"))
    with pytest.raises(ValueError):
        applier.validate(CodeChange(path="config.py", content="-----BEGIN RSA PRIVATE KEY-----\n"))
    with pytest.raises(ValueError):
        applier.validate(CodeChange(path=".env", content="DATABASE_URL=x\n"))
    with pytest.raises(ValueError):
        applier.validate(CodeChange(path="credentials.json", content="{}"))


def test_applier_rejects_invalid_python_and_oversized(tmp_path):
    applier = _applier(tmp_path)
    with pytest.raises(ValueError):
        applier.validate(CodeChange(path="app.py", content="def broken(:\n"))
    with pytest.raises(ValueError):
        applier.validate(CodeChange(path="big.py", content="x = 1\n" * (2 * 1024 * 1024)))


def test_applier_rejects_unsupported_action(tmp_path):
    applier = _applier(tmp_path)
    with pytest.raises(ValueError):
        applier.validate(CodeChange(path="app.py", content="x = 1\n", action="delete"))


# -- application + checkpoint + rollback ------------------------------------

def test_applier_applies_changes_and_records_paths(tmp_path):
    (tmp_path / "app.py").write_text("original\n")
    (tmp_path / "unrelated.txt").write_text("keep me\n")
    applier = _applier(tmp_path)

    result = applier.apply([
        CodeChange(path="app.py", content="modified\n", action="modify"),
        CodeChange(path="new_mod.py", content="def f(): return 1\n", action="create"),
    ], approved=True)

    assert result.success
    assert result.changed_paths == ["app.py", "new_mod.py"]
    assert result.checkpoint_id
    assert (tmp_path / "app.py").read_text() == "modified\n"
    assert (tmp_path / "new_mod.py").read_text() == "def f(): return 1\n"
    assert (tmp_path / "unrelated.txt").read_text() == "keep me\n"
    applier.checkpoint_manager.cleanup(result.checkpoint)


def test_applier_rollback_restores_exact_state(tmp_path):
    (tmp_path / "app.py").write_text("original\n")
    applier = _applier(tmp_path)

    result = applier.apply([
        CodeChange(path="app.py", content="modified\n"),
        CodeChange(path="new_mod.py", content="x = 1\n", action="create"),
    ], approved=True)
    assert result.success

    applier.rollback(result)

    assert (tmp_path / "app.py").read_text() == "original\n"
    assert not (tmp_path / "new_mod.py").exists()


def test_applier_requires_approval_for_writes(tmp_path):
    (tmp_path / "app.py").write_text("original\n")
    applier = _applier(tmp_path, mode=OperationMode.ASSISTED)

    result = applier.apply([CodeChange(path="app.py", content="changed\n")], approved=False)

    assert not result.success
    assert result.changed_paths == []
    assert (tmp_path / "app.py").read_text() == "original\n"


def test_applier_failed_apply_leaves_no_partial_writes(tmp_path):
    (tmp_path / "app.py").write_text("original\n")
    applier = _applier(tmp_path)

    result = applier.apply([
        CodeChange(path="app.py", content="changed\n"),
        CodeChange(path="../outside.py", content="x = 1\n"),
    ], approved=True)

    assert not result.success
    # The invalid second change is rejected and the first is rolled back.
    assert (tmp_path / "app.py").read_text() == "original\n"
    assert not (tmp_path.parent / "outside.py").exists()
