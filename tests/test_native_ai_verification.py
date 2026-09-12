"""Verification gates: measured, bounded, and failures stay failures."""
from __future__ import annotations

from helpers_native_ai import write_repo

from forge.native.verification import NativeVerifier


def test_clean_fixture_passes_all_configured_gates(tmp_path):
    write_repo(tmp_path)
    report = NativeVerifier(tmp_path).verify(
        changed_files=["calc.py", "test_calc.py"],
        diff_text="def add(a, b):\n    return a + b\n")
    assert report.all_passed, report.to_dict()
    assert report.status == "PASS"
    names = {gate.name for gate in report.gates}
    assert {"compile", "tests", "build", "lint/type", "security",
            "diff_validation"} <= names


def test_syntax_error_fails_compile_and_cannot_be_averaged_away(tmp_path):
    write_repo(tmp_path)
    (tmp_path / "broken.py").write_text("def oops(:\n", encoding="utf-8")
    verifier = NativeVerifier(tmp_path)
    gate = verifier.compile_check(["broken.py"])
    assert not gate.passed
    assert "broken.py" in gate.evidence["problems"][0]["file"]
    # aggregate keeps it red even though every other gate passes:
    report = verifier.verify(changed_files=["broken.py", "calc.py",
                                            "test_calc.py"],
                             diff_text="def oops(:")
    assert not report.all_passed
    assert "compile" in report.failed


def test_failing_tests_gate_stays_failed(tmp_path):
    write_repo(tmp_path, calc="def add(a, b):\n    return a ^ b\n",
               test_body="def test_add():\n    assert add(1, 2) == 999\n"
                         .replace("add", "calc.add"))
    report = NativeVerifier(tmp_path).verify(
        changed_files=None, diff_text="", run_build=False, run_lint=False)
    tests = [g for g in report.gates if g.name == "tests"][0]
    assert tests.executed and not tests.passed
    assert report.status == "FAIL"
    assert "tests" in report.failed


def test_unconfigured_lint_is_reported_not_hidden(tmp_path):
    write_repo(tmp_path)
    verifier = NativeVerifier(tmp_path)
    gate = verifier.pipeline.lint()
    assert gate.passed  # the pipeline's recorded pass-when-unconfigured
    report = verifier.verify(changed_files=["calc.py", "test_calc.py"],
                             diff_text="def add(a, b):\n    return a + b\n",
                             run_build=False)
    lint = [g for g in report.gates if g.name == "lint/type"][0]
    assert lint.executed
    assert lint.evidence.get("configured") is False  # surfaced, not erased


def test_diff_validation_flags_unsafe_and_phantom_declared_paths(tmp_path):
    write_repo(tmp_path)
    verifier = NativeVerifier(tmp_path)
    gate = verifier.diff_validation(["../escape.py", ".forge/state.json",
                                     "missing.py"],
                                    "diff text without markers")
    assert not gate.passed
    reasons = " ".join(gate.evidence["issues"])
    assert "unsafe" in reasons
    assert "protected runtime directory" in reasons
    assert "missing from the repository" in reasons


def test_diff_validation_flags_conflict_markers(tmp_path):
    write_repo(tmp_path)
    gate = NativeVerifier(tmp_path).diff_validation(
        ["calc.py"], "++<<<<<<< HEAD\n++some change")
    assert not gate.passed
    assert "conflict" in gate.evidence["issues"][0]


def test_diff_validation_without_git_is_skipped_not_passed(tmp_path):
    write_repo(tmp_path)
    gate = NativeVerifier(tmp_path).diff_validation(
        ["calc.py"], "", material_available=False)
    assert not gate.executed
    assert not gate.passed
    assert "skipped" in gate.details


def test_security_gate_fails_on_secret_material(tmp_path):
    write_repo(tmp_path, extra={"leak.py":
                                "api_key = \"sk-live-1234567890abcdef\"\n"})
    verifier = NativeVerifier(tmp_path)
    gate = verifier.pipeline.security(["leak.py"])
    assert not gate.passed


def test_scoped_off_tests_gate_reports_not_executed(tmp_path):
    write_repo(tmp_path)
    report = NativeVerifier(tmp_path).verify(
        changed_files=["calc.py", "test_calc.py"],
        diff_text="def add(a, b):\n    return a + b\n",
        run_tests=False, run_build=False)
    tests = [g for g in report.gates if g.name == "tests"][0]
    assert not tests.executed and not tests.passed
    assert "tests" in report.skipped
    # skipped gates never flip the aggregate to a silent pass
    assert report.status == "PARTIAL"
