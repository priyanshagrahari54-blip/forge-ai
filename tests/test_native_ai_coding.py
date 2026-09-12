"""Coding engine: propose → validate → authorize → apply, nothing skipped.

These tests pin the security-relevant wiring: every write of the native
engine goes through the A32 ChangeSet engine and the A33 policy gate. A
denial is a denial; approval from the caller satisfies REQUIRE_APPROVAL but
never DENY; unsafe paths, secrets, and invalid Python never reach the disk;
and without a model, *no* code appears from anywhere.
"""
from __future__ import annotations

import sys

from helpers_native_ai import (
    CALC_BROKEN,
    CALC_GOOD,
    changes_text,
    scripted_fabric,
    write_repo,
)

from forge.native.engine import NativeAIEngine
from forge.security.permissions import OperationMode


def coding_engine(tmp_path, mode=OperationMode.ASSISTED, responder=None):
    engine = NativeAIEngine(tmp_path, mode=mode,
                            fabric=scripted_fabric(
                                responder if responder is not None else {})
                            if responder is not None else None,
                            persist_status=False)
    return engine, engine.coding


def test_refuses_to_generate_without_a_model(tmp_path):
    write_repo(tmp_path)
    engine, coding = coding_engine(tmp_path)
    proposal = coding.propose_changes("implement multiply in calc.py", "")
    assert not proposal.ok
    assert proposal.requires_model
    assert not (tmp_path / "calc.py").read_text() != CALC_GOOD


def test_proposal_parses_valid_model_json(tmp_path):
    write_repo(tmp_path, calc=CALC_BROKEN)

    def responder(prompt):
        return changes_text({"calc.py": CALC_GOOD})

    engine, coding = coding_engine(tmp_path, responder=responder)
    proposal = coding.propose_changes("fix calc.py add", "")
    assert proposal.ok, proposal.to_dict()
    assert proposal.changes == {"calc.py": CALC_GOOD}
    assert proposal.model == "scripted-model"
    assert proposal.generated_by == "local-neural(scripted-model)"


def test_validation_rejects_unsafe_paths_and_secrets(tmp_path):
    write_repo(tmp_path)
    engine, coding = coding_engine(tmp_path)
    report = coding.validate_changes({
        "../outside.py": "x = 1\n",
        ".forge/state.json": "{}",
        "leak.py": 'password = "hunter22222222"\n',
    })
    assert not report.ok
    assert len(report.issues) == 3  # structured, per-path failures
    assert report.would_change == []  # nothing is marked writable
    assert not (tmp_path.parent / "outside.py").exists()
    assert not (tmp_path / "leak.py").exists()


def test_apply_needs_approval_in_assisted_and_denies_in_safe(tmp_path):
    write_repo(tmp_path)
    _engine, assisted = coding_engine(tmp_path)
    outcome = assisted.apply_changes({"new.py": "VALUE = 1\n"})
    assert not outcome.ok and outcome.blocked_by_approval
    assert not (tmp_path / "new.py").exists()

    _safe_engine, safe = coding_engine(tmp_path, mode=OperationMode.SAFE)
    denied = safe.apply_changes({"new.py": "VALUE = 1\n"}, approved=True)
    assert not denied.ok
    assert not (tmp_path / "new.py").exists()


def test_authorized_apply_writes_and_rolls_back_exactly(tmp_path):
    write_repo(tmp_path, calc=CALC_BROKEN)
    engine, coding = coding_engine(tmp_path)
    coding.open_checkpoint()
    before = (tmp_path / "calc.py").read_text(encoding="utf-8")
    outcome = coding.apply_changes({"calc.py": CALC_GOOD}, approved=True,
                                   label="test")
    assert outcome.ok, outcome.to_dict()
    assert outcome.files == ["calc.py"]
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == CALC_GOOD
    assert coding.rollback(["calc.py"]) is True
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == before


def test_invalid_python_proposal_never_applies(tmp_path):
    write_repo(tmp_path)

    def responder(prompt):
        return changes_text({"calc.py": "def broken(:\n"})

    engine, coding = coding_engine(tmp_path, responder=responder)
    proposal = coding.propose_changes("fix calc.py", "")
    assert proposal.ok  # it is model output; parsing is not the guard
    validation = coding.validate_changes(proposal.changes)
    assert not validation.ok
    assert any("python" in issue.reason.lower() or "syntax" in
               issue.reason.lower() for issue in validation.issues)


def test_run_tests_reports_real_exit_codes(tmp_path):
    write_repo(tmp_path, calc=CALC_BROKEN)
    engine, coding = coding_engine(tmp_path)
    failing = coding.run_tests(["test_calc.py"])
    assert failing.executed and not failing.passed
    assert failing.exit_code != 0
    assert "assert" in failing.output.lower() or "1" in failing.output


def test_run_tests_rejects_unsafe_paths_and_keeps_full_suite(tmp_path):
    write_repo(tmp_path)
    engine, coding = coding_engine(tmp_path)
    command, accepted = coding.test_command(["test_calc.py",
                                             "../escape.py",
                                             ".forge/x.py",
                                             "C:\\evil.py"])
    assert accepted == ["test_calc.py"]
    assert command[:3] == [sys.executable, "-B", "-m"]
    assert command[3] == "pytest"


def test_inspection_goes_through_the_permissioned_runtime(tmp_path):
    write_repo(tmp_path)
    engine, coding = coding_engine(tmp_path)
    ok = coding.inspect_file("calc.py")
    assert ok.ok and "def add" in ok.content
    escaped = coding.inspect_file("../secrets.env")
    assert not escaped.ok
    missing = coding.inspect_file("no_such_file.py")
    assert not missing.ok
