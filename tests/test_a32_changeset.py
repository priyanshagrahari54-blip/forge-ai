"""Hardened ChangeSet engine tests (A32.1 rebuild).

Covers deterministic fingerprints, dry-run validation without writes,
old-content/hash guards, structured errors, and the explicitly-gated delete
path. The legacy ``test_a32_change_applier.py`` suite keeps passing unchanged.
"""
import hashlib

import pytest

from forge.runtime.defaults import create_default_runtime
from forge.security.permissions import OperationMode, PermissionManager
from forge.tools.change_applier import ChangeApplier, CodeChange
from forge.tools.checkpoint import CheckpointManager


def _applier(root, mode=OperationMode.ASSISTED, with_checkpoint=True):
    runtime = create_default_runtime(PermissionManager(mode=mode), str(root))
    manager = CheckpointManager(root) if with_checkpoint else None
    return ChangeApplier(runtime, manager, root=str(root))


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- fingerprint ----------------------------------------------------------

def test_fingerprint_is_deterministic_and_order_independent():
    first = [
        CodeChange(path="a.py", content="x = 1\n"),
        CodeChange(path="b.py", content="y = 2\n", action="create"),
    ]
    second = list(reversed(first))
    assert ChangeApplier.fingerprint(first) == ChangeApplier.fingerprint(second)
    assert len(ChangeApplier.fingerprint(first)) == 64


def test_fingerprint_is_sensitive_to_every_byte():
    base = [CodeChange(path="a.py", content="x = 1\n")]
    assert ChangeApplier.fingerprint(base) != ChangeApplier.fingerprint(
        [CodeChange(path="a.py", content="x = 2\n")])
    assert ChangeApplier.fingerprint(base) != ChangeApplier.fingerprint(
        [CodeChange(path="b.py", content="x = 1\n")])
    assert ChangeApplier.fingerprint(base) != ChangeApplier.fingerprint(
        [CodeChange(path="a.py", content="x = 1\n", action="create")])


def test_fingerprint_accepts_dict_proposals():
    proposal = [{"path": "a.py", "content": "x = 1\n"}]
    assert ChangeApplier.fingerprint(proposal) == ChangeApplier.fingerprint(
        [CodeChange(path="a.py", content="x = 1\n")])


# -- dry-run --------------------------------------------------------------

def test_dry_run_validates_without_writing(tmp_path):
    (tmp_path / "app.py").write_text("original\n")
    applier = _applier(tmp_path)
    result = applier.dry_run([
        CodeChange(path="app.py", content="changed\n"),
        CodeChange(path="new_mod.py", content="x = 1\n", action="create"),
    ])
    assert result.valid
    assert result.would_change == ["app.py", "new_mod.py"]
    assert result.fingerprint
    # No write, no checkpoint side effect, no new file.
    assert (tmp_path / "app.py").read_text() == "original\n"
    assert not (tmp_path / "new_mod.py").exists()


def test_dry_run_reports_errors_without_touching_valid_files(tmp_path):
    (tmp_path / "app.py").write_text("original\n")
    applier = _applier(tmp_path)
    result = applier.dry_run([
        CodeChange(path="app.py", content="changed\n"),
        CodeChange(path="../outside.py", content="x = 1\n"),
    ])
    assert not result.valid
    assert result.would_change == ["app.py"]
    assert any(detail.code == "UNSAFE_PATH" for detail in result.error_details)
    assert (tmp_path / "app.py").read_text() == "original\n"


# -- old-state guards -----------------------------------------------------

def test_old_hash_guard_allows_exact_match(tmp_path):
    (tmp_path / "app.py").write_text("original\n")
    applier = _applier(tmp_path)
    result = applier.apply([
        CodeChange(path="app.py", content="changed\n",
                   expected_old_hash=_sha("original\n")),
    ], approved=True)
    assert result.success
    assert (tmp_path / "app.py").read_text() == "changed\n"


def test_old_hash_mismatch_rejects_without_writing(tmp_path):
    (tmp_path / "app.py").write_text("original\n")
    applier = _applier(tmp_path)
    result = applier.apply([
        CodeChange(path="app.py", content="changed\n",
                   expected_old_hash=_sha("something else\n")),
    ], approved=True)
    assert not result.success
    assert result.error_details[0].code == "OLD_STATE_MISMATCH"
    assert (tmp_path / "app.py").read_text() == "original\n"


def test_old_content_guard_matches_and_mismatches(tmp_path):
    (tmp_path / "app.py").write_text("original\n")
    applier = _applier(tmp_path)
    assert applier.dry_run([
        CodeChange(path="app.py", content="x\n", expected_old_content="original\n"),
    ]).valid
    assert not applier.dry_run([
        CodeChange(path="app.py", content="x\n", expected_old_content="stale\n"),
    ]).valid


def test_old_guard_on_missing_file_rejects(tmp_path):
    applier = _applier(tmp_path)
    result = applier.dry_run([
        CodeChange(path="ghost.py", content="x = 1\n",
                   expected_old_hash=_sha("original\n")),
    ])
    assert not result.valid
    assert result.error_details[0].code == "OLD_STATE_MISMATCH"


def test_old_guard_without_root_is_unverifiable(tmp_path):
    runtime = create_default_runtime(PermissionManager(), str(tmp_path))
    applier = ChangeApplier(runtime)  # no root: old state cannot be read
    result = applier.dry_run([
        CodeChange(path="app.py", content="x = 1\n",
                   expected_old_hash=_sha("original\n")),
    ])
    assert not result.valid
    assert result.error_details[0].code == "OLD_STATE_UNVERIFIABLE"


# -- structured errors ----------------------------------------------------

def test_apply_records_structured_error_codes(tmp_path):
    applier = _applier(tmp_path)
    result = applier.apply([
        {"path": "bad.py", "content": "def broken(:\n"},
        {"path": ".env", "content": "X=1\n"},
    ], approved=True)
    assert not result.success
    assert {detail.code for detail in result.error_details} == {
        "INVALID_PYTHON", "ENV_FILE"}
    assert result.fingerprint == ChangeApplier.fingerprint([
        {"path": "bad.py", "content": "def broken(:\n"},
        {"path": ".env", "content": "X=1\n"},
    ])


# -- security regressions ---------------------------------------------------

@pytest.mark.parametrize("bad_path", [
    "src/.git/config",
    "nested/.forge/state.json",
    "a/b/../../escape.py",
])
def test_rejects_nested_protected_and_escape_paths(tmp_path, bad_path):
    applier = _applier(tmp_path)
    with pytest.raises(ValueError):
        applier.validate(CodeChange(path=bad_path, content="x = 1\n"))


def test_fingerprint_is_sensitive_to_old_state_guards():
    base = [CodeChange(path="a.py", content="x = 1\n")]
    guarded = [CodeChange(path="a.py", content="x = 1\n",
                          expected_old_hash="abc")]
    assert ChangeApplier.fingerprint(base) != ChangeApplier.fingerprint(guarded)


def test_old_hash_match_is_case_insensitive(tmp_path):
    (tmp_path / "app.py").write_text("original\n")
    applier = _applier(tmp_path)
    result = applier.dry_run([
        CodeChange(path="app.py", content="changed\n",
                   expected_old_hash=_sha("original\n").upper()),
    ])
    assert result.valid


# -- gated delete ---------------------------------------------------------

def test_delete_rejected_by_default(tmp_path):
    (tmp_path / "app.py").write_text("original\n")
    applier = _applier(tmp_path)
    with pytest.raises(ValueError):
        applier.validate(CodeChange(path="app.py", content="", action="delete"))
    result = applier.apply(
        [CodeChange(path="app.py", content="", action="delete")], approved=True)
    assert not result.success
    assert result.error_details[0].code == "DELETE_REQUIRES_APPROVAL"
    assert (tmp_path / "app.py").read_text() == "original\n"


def test_delete_with_content_rejected_even_when_allowed(tmp_path):
    applier = _applier(tmp_path)
    with pytest.raises(ValueError):
        applier.validate(
            CodeChange(path="app.py", content="x = 1\n", action="delete"),
            allow_delete=True)


def test_delete_requires_explicit_approval(tmp_path):
    (tmp_path / "app.py").write_text("original\n")
    applier = _applier(tmp_path)
    result = applier.apply(
        [CodeChange(path="app.py", content="", action="delete")],
        approved=False, allow_delete=True)
    assert not result.success
    assert (tmp_path / "app.py").read_text() == "original\n"


def test_delete_applies_and_rolls_back_exactly(tmp_path):
    (tmp_path / "app.py").write_text("original\n")
    (tmp_path / "keep.txt").write_text("keep me\n")
    applier = _applier(tmp_path)
    result = applier.apply(
        [CodeChange(path="app.py", content="", action="delete")],
        approved=True, allow_delete=True)
    assert result.success
    assert result.changed_paths == ["app.py"]
    assert not (tmp_path / "app.py").exists()

    applier.rollback(result)
    assert (tmp_path / "app.py").read_text() == "original\n"
    assert (tmp_path / "keep.txt").read_text() == "keep me\n"


def test_delete_rejected_in_safe_mode(tmp_path):
    (tmp_path / "app.py").write_text("original\n")
    applier = _applier(tmp_path, mode=OperationMode.SAFE)
    result = applier.apply(
        [CodeChange(path="app.py", content="", action="delete")],
        approved=True, allow_delete=True)
    assert not result.success
    assert (tmp_path / "app.py").read_text() == "original\n"
