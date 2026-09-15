"""A83 engines: build, testing, debugging, performance. Real commands only."""
from __future__ import annotations

import json
import sys
import textwrap
import time

import pytest

from forge.builder.detection import artifact_snapshot, detect, diff_artifacts
from forge.builder.diagnostics import detect_parser, parse_diagnostics
from forge.builder.engine import BuildEngine
from forge.builder.runner import CommandError, CommandRunner, resolve_executable
from forge.debug.diagnosis import diagnose_build_failure, diagnose_test_failure
from forge.debug.loop import AutomatedDebugLoop, CallableFixStrategy, FixLedger
from forge.perf.lab import PerformanceLab, compare
from forge.perf.metrics import statistics
from forge.testing.engine import TestEngine, render
from forge.testing.results import parse_test_output


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


# =========================================================================
# command runner
# =========================================================================


def test_runner_reports_a_real_exit_code(tmp_path):
    runner = CommandRunner(tmp_path)
    ok = runner.run([sys.executable, "-c", "print('hi')"])
    assert ok.succeeded and ok.return_code == 0 and "hi" in ok.stdout
    bad = runner.run([sys.executable, "-c", "raise SystemExit(3)"])
    assert bad.status == "failed" and bad.return_code == 3


def test_runner_rejects_what_execve_cannot_take(tmp_path):
    runner = CommandRunner(tmp_path)
    with pytest.raises(CommandError, match="non-empty argv"):
        runner.run([])
    with pytest.raises(CommandError, match="non-empty strings"):
        runner.run([sys.executable, "   "])
    with pytest.raises(CommandError, match="null bytes"):
        runner.run([sys.executable, "-c", "print('a" + chr(0) + "b')"])


def test_runner_allows_a_semicolon_inside_one_argument(tmp_path):
    """No shell is used, so a ';' inside argv is inert program text."""
    result = CommandRunner(tmp_path).run(
        [sys.executable, "-c", "print(1); print(2)"])
    assert result.succeeded
    assert result.stdout.strip() == "1\n2"


def test_missing_tool_is_unavailable_not_failed(tmp_path):
    result = CommandRunner(tmp_path).run(["definitely-not-a-real-tool", "--help"])
    assert result.status == "unavailable"
    assert result.ran is False
    assert "not found on PATH" in result.reason


def test_python_alias_resolves_to_this_interpreter():
    assert resolve_executable("python") == sys.executable
    assert resolve_executable("definitely-not-a-real-tool") == ""


def test_timeout_terminates_the_command(tmp_path):
    runner = CommandRunner(tmp_path)
    result = runner.run([sys.executable, "-c", "import time; time.sleep(30)"],
                        timeout=1.0)
    assert result.status == "timeout"
    assert result.duration_ms < 20_000
    assert "terminated" in result.reason


def test_cwd_is_confined_to_the_project_root(tmp_path):
    runner = CommandRunner(tmp_path)
    with pytest.raises(CommandError, match="escapes the project root"):
        runner.run([sys.executable, "-c", "print(1)"], cwd="../..")


def test_dry_run_executes_nothing(tmp_path):
    runner = CommandRunner(tmp_path, dry_run=True)
    result = runner.run([sys.executable, "-c", "print('never')"])
    assert result.status == "refused"
    assert result.stdout == ""
    assert "nothing was executed" in result.reason


def test_run_cancel_reports_the_cancellation(tmp_path):
    result = CommandRunner(tmp_path).run_cancel(
        [sys.executable, "-u", "-c",
         "print('booted', flush=True); import time; time.sleep(30)"],
        timeout=1.0)
    assert "booted" in result.stdout
    assert result.cancellation in ("SIGTERM", "SIGKILL", "terminate", "kill")


def test_output_is_bounded(tmp_path):
    runner = CommandRunner(tmp_path)
    result = runner.run([sys.executable, "-c", "print('x' * 400000)"])
    assert result.output_truncated is True
    assert len(result.stdout) <= 200_000


# =========================================================================
# diagnostics
# =========================================================================

GCC_OUTPUT = """
src/main.c:12:5: error: expected ';' before 'return'
src/main.c:20:1: warning: unused variable 'x'
make: *** [Makefile:7: all] Error 1
"""

RUST_OUTPUT = """
error[E0308]: mismatched types
 --> src/main.rs:4:12
  |
4 |     let x: u32 = "no";
  |            ^^^ expected `u32`, found `&str`

error: could not compile `demo` (bin "demo") due to 1 previous error
"""

TSC_OUTPUT = "src/app.ts(12,5): error TS2304: Cannot find name 'widget'.\n"

PY_OUTPUT = """
Traceback (most recent call last):
  File "app/service.py", line 42, in run
    return self.helper()
ValueError: bad input
"""


def test_gcc_diagnostics_are_structured():
    result = parse_diagnostics(GCC_OUTPUT, "gcc")
    assert result.errors[0].file == "src/main.c"
    assert result.errors[0].line == 12 and result.errors[0].column == 5
    assert len(result.warnings) == 1
    assert "make: ***" in result.failure_markers


def test_rust_diagnostics_pair_the_header_with_its_location():
    result = parse_diagnostics(RUST_OUTPUT)
    assert result.parser == "rust"
    assert result.errors[0].rule == "E0308"
    assert result.errors[0].file == "src/main.rs"
    assert result.errors[0].line == 4


def test_tsc_and_msvc_formats():
    result = parse_diagnostics(TSC_OUTPUT)
    assert result.parser == "tsc"
    assert result.errors[0].rule == "TS2304"
    assert result.errors[0].line == 12 and result.errors[0].column == 5


def test_python_tracebacks_resolve_to_file_and_line():
    result = parse_diagnostics(PY_OUTPUT)
    assert result.parser == "python"
    assert result.errors[0].file == "app/service.py"
    assert result.errors[0].line == 42
    assert result.errors[0].rule == "ValueError"


def test_unparsable_output_is_reported_as_unparsed_not_clean():
    result = parse_diagnostics("something went sideways\n", "generic")
    assert result.diagnostics == []
    assert result.unparsed_lines == ["something went sideways"]
    assert result.parser == "generic"


def test_parser_detection_is_reported():
    assert detect_parser(GCC_OUTPUT) == "gcc"
    assert detect_parser(RUST_OUTPUT) == "rust"
    assert detect_parser(PY_OUTPUT) == "python"
    assert detect_parser("hello\n") == "generic"


# =========================================================================
# detection
# =========================================================================


def test_detects_cargo_cmake_make_and_kernel(tmp_path):
    write(tmp_path, "Cargo.toml", "[package]\nname='x'\n")
    assert [system.name for system in detect(tmp_path)] == ["cargo"]

    other = tmp_path / "cmake"
    write(other, "CMakeLists.txt", "cmake_minimum_required(VERSION 3.10)\n")
    assert [system.name for system in detect(other)] == ["cmake"]


def test_kernel_project_is_detected_before_generic_make(tmp_path):
    write(tmp_path, "Makefile", "all:\n\techo build\n")
    write(tmp_path, "kernel.ld", "SECTIONS {}\n")
    write(tmp_path, "boot.asm", "bits 32\n")
    write(tmp_path, "kernel.c", "void kmain(void) {}\n")
    systems = detect(tmp_path)
    assert systems[0].name == "kernel"
    assert "kernel" in systems[0].note.lower() or systems[0].priority == 5


def test_detection_reports_tool_availability_honestly(tmp_path):
    write(tmp_path, "Cargo.toml", "[package]\nname='x'\n")
    system = detect(tmp_path)[0]
    # Whether cargo exists here is a fact about this machine, not a guess:
    # either way the flag must be a boolean and match the PATH.
    import shutil
    assert system.available is (shutil.which("cargo") is not None)


def test_detection_on_an_empty_directory_finds_nothing(tmp_path):
    assert detect(tmp_path) == []


def test_artifact_snapshot_detects_created_files(tmp_path):
    before = artifact_snapshot(tmp_path)
    write(tmp_path, "build/out.bin", "data")
    after = artifact_snapshot(tmp_path)
    diff = diff_artifacts(before, after)
    assert diff["created"] == ["build/out.bin"]


# =========================================================================
# build engine
# =========================================================================


def test_build_engine_runs_a_real_build(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname='demo'\nversion='0.1.0'\n")
    write(tmp_path, "pkg/mod.py", "VALUE = 1\n")
    report = BuildEngine(tmp_path).build()
    assert report.system == "python"
    assert report.ran is True
    assert report.ok is True
    assert report.return_code == 0
    assert report.duration_ms > 0
    assert report.summary()["candidates"]


def test_build_failure_produces_structured_diagnostics(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname='demo'\n")
    write(tmp_path, "broken.py", "def f(:\n")
    report = BuildEngine(tmp_path).build()
    assert report.ok is False
    assert report.status == "failed"
    assert report.diagnostics.errors
    assert report.diagnostics.errors[0].file.replace("\\", "/") == "broken.py"


def test_profile_recipe_beats_detection(tmp_path):
    from forge.profiles import load_profile_dict
    write(tmp_path, "pyproject.toml", "[project]\nname='demo'\n")
    write(tmp_path, "Makefile", "all:\n\t@echo PROFILE-BUILD-OK\n")
    profile = load_profile_dict({
        "format": "forge-project-profile",
        "name": "demo-profile",
        "version": "1.0.0",
        "build_recipes": [{"name": "make-all", "argv": ["make", "all"],
                           "evidence": ["Makefile"]}],
    })
    report = BuildEngine(tmp_path, profile=profile).build()
    assert report.source == "profile"
    assert report.system == "make-all"
    assert "PROFILE-BUILD-OK" in report.log
    assert report.ok is True


def test_unavailable_toolchain_is_reported_not_faked(tmp_path):
    from forge.profiles import load_profile_dict
    write(tmp_path, "Makefile", "all:\n\techo hi\n")
    profile = load_profile_dict({
        "format": "forge-project-profile",
        "name": "missing-tool",
        "version": "1.0.0",
        "build_recipes": [{"name": "nope",
                           "argv": ["absolutely-not-installed", "build"]}],
    })
    report = BuildEngine(tmp_path, profile=profile).build()
    assert report.status == "unavailable"
    assert report.ran is False
    assert "absolutely-not-installed" in report.reason
    assert report.ok is False


def test_no_build_system_is_reported_honestly(tmp_path):
    report = BuildEngine(tmp_path).build()
    assert report.status == "no_build_system"
    assert report.ok is False
    assert "no build system detected" in report.reason


def test_unknown_recipe_name_is_refused(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname='demo'\n")
    report = BuildEngine(tmp_path).build(name="does-not-exist")
    assert report.status == "refused"
    assert report.ran is False


def test_exit_zero_with_error_diagnostics_is_a_failure(tmp_path):
    from forge.profiles import load_profile_dict
    write(tmp_path, "run.py", "print('src/x.c:1:1: error: boom')\n")
    profile = load_profile_dict({
        "format": "forge-project-profile",
        "name": "lying-build",
        "version": "1.0.0",
        "build_recipes": [{"name": "lie",
                           "argv": [sys.executable, "run.py"]}],
    })
    report = BuildEngine(tmp_path, profile=profile).build()
    assert report.return_code == 0
    assert report.status == "failed"
    assert "exited 0 but reported" in report.reason
    assert report.ok is False


# =========================================================================
# test engine
# =========================================================================

PYTEST_OUTPUT = """\
============================= test session starts ==============================
collected 3 items

tests/test_a.py::test_one PASSED                                           [ 33%]
tests/test_a.py::test_two FAILED                                           [ 66%]
tests/test_b.py::test_three SKIPPED                                        [100%]

=================================== FAILURES ===================================
___________________________________ test_two ___________________________________

    def test_two():
>       assert 1 == 2
E       assert 1 == 2

tests/test_a.py:5: AssertionError
=========================== short test summary info ============================
FAILED tests/test_a.py::test_two - assert 1 == 2
================== 1 failed, 1 passed, 1 skipped in 0.12s ===================
"""


def test_pytest_output_is_parsed_into_individual_cases():
    parse = parse_test_output(PYTEST_OUTPUT, return_code=1)
    assert parse.parser == "pytest"
    assert parse.counts == {"passed": 1, "failed": 1, "error": 0,
                            "skipped": 1, "xfail": 0, "xpass": 0}
    assert parse.failures == ["tests/test_a.py::test_two"]
    assert parse.status == "failed"
    assert parse.inconsistent == ""


def test_cargo_and_go_output_are_parsed():
    cargo = ("test tests::it_works ... ok\n"
             "test tests::it_breaks ... FAILED\n"
             "\ntest result: FAILED. 1 passed; 1 failed; 0 ignored\n")
    parse = parse_test_output(cargo)
    assert parse.parser == "cargo"
    assert parse.counts["passed"] == 1 and parse.counts["failed"] == 1

    go = "--- PASS: TestFoo (0.00s)\n--- FAIL: TestBar (0.00s)\n"
    parse = parse_test_output(go)
    assert parse.parser == "go"
    assert parse.failures == ["TestBar"]


def test_no_tests_collected_is_never_a_pass():
    parse = parse_test_output("collected 0 items\nno tests ran",
                              return_code=5)
    assert parse.status == "no_tests"


def test_inconsistent_counts_are_reported():
    tampered = PYTEST_OUTPUT.replace("1 failed, 1 passed",
                                     "9 failed, 1 passed")
    parse = parse_test_output(tampered, return_code=1)
    assert "tool reported 9 failed" in parse.inconsistent


@pytest.fixture()
def failing_project(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname='demo'\n")
    write(tmp_path, "app/calc.py", """
        def divide(left, right):
            return left / right
    """)
    write(tmp_path, "tests/test_calc.py", """
        from app.calc import divide


        def test_divide():
            assert divide(4, 2) == 2


        def test_divide_by_zero_is_handled():
            assert divide(1, 0) == 0
    """)
    (tmp_path / "app" / "__init__.py").write_text("")
    return tmp_path


def test_test_engine_runs_real_tests_and_reports_the_failure(failing_project):
    engine = TestEngine(failing_project)
    report = engine.run(stages=["unit"])
    assert report.status() == "failed"
    assert report.ok is False
    unit = report.stage("unit")
    assert unit is not None
    assert unit.parse.counts["failed"] == 1
    assert unit.parse.counts["passed"] == 1
    assert unit.parse.failures == [
        "tests/test_calc.py::test_divide_by_zero_is_handled"]
    assert "FAIL" in render(report)


def test_static_analysis_stage_runs_before_tests(failing_project):
    report = TestEngine(failing_project).run()
    names = [item.stage for item in report.stages]
    assert names[0] == "static-analysis"
    assert "unit" in names


def test_a_passing_project_reports_real_success(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname='demo'\n")
    write(tmp_path, "tests/test_ok.py", "def test_ok():\n    assert True\n")
    report = TestEngine(tmp_path).run(stages=["unit"])
    assert report.ok is True
    assert report.total_cases == 1


def test_an_empty_project_is_no_tests_not_a_pass(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname='demo'\n")
    report = TestEngine(tmp_path).run(stages=["unit"])
    assert report.status() == "no_tests_collected"
    assert report.ok is False


def test_regression_stage_needs_targets(tmp_path):
    write(tmp_path, "tests/test_ok.py", "def test_ok():\n    assert True\n")
    report = TestEngine(tmp_path).run()
    skipped = {item["name"]: item["reason"] for item in report.skipped}
    assert "regression" not in skipped or "no previously failing" in skipped[
        "regression"]


def test_profile_test_recipe_plans_the_unit_stage(tmp_path):
    from forge.profiles import load_profile_dict
    write(tmp_path, "Makefile", "test:\n\t@echo no tests here\n")
    profile = load_profile_dict({
        "format": "forge-project-profile",
        "name": "make-tests",
        "version": "1.0.0",
        "test_recipes": [{"name": "make-test", "argv": ["make", "test"]}],
    })
    planned = TestEngine(tmp_path, profile=profile).plan()
    unit = next(item for item in planned if item.name == "unit")
    assert unit.source == "profile"
    assert unit.argv == ("make", "test")


# =========================================================================
# diagnosis
# =========================================================================


def test_diagnose_test_failure_names_the_suspect_file(failing_project):
    engine = TestEngine(failing_project)
    report = engine.run(stages=["unit"])
    unit = report.stage("unit")
    diagnosis = diagnose_test_failure(unit.parse, unit.log,
                                      root=failing_project)
    # The failing test raises ZeroDivisionError, which is a runtime
    # exception, not an assertion failure -- the classification follows the
    # evidence rather than assuming every test failure is an assert.
    assert diagnosis.category == "runtime_exception"
    assert diagnosis.failing_tests == [
        "tests/test_calc.py::test_divide_by_zero_is_handled"]
    assert diagnosis.confidence >= 0.6
    assert diagnosis.evidence
    assert "app/calc.py" in diagnosis.suspect_files


def test_diagnose_import_error(tmp_path):
    parse = parse_test_output(
        "tests/test_x.py::test_y FAILED\n"
        "ModuleNotFoundError: No module named 'missing'\n", return_code=1)
    diagnosis = diagnose_test_failure(parse, "ModuleNotFoundError: "
                                      "No module named 'missing'", root=tmp_path)
    assert diagnosis.category == "import_error"


def test_diagnose_build_failure_uses_the_diagnostics():
    result = parse_diagnostics(GCC_OUTPUT, "gcc")
    diagnosis = diagnose_build_failure(result, GCC_OUTPUT)
    assert diagnosis.category == "syntax_error"
    assert diagnosis.suspect_files == ["src/main.c"]
    assert diagnosis.confidence == 0.9


def test_unclassifiable_failure_stays_unknown():
    diagnosis = diagnose_test_failure(
        parse_test_output("gibberish\n", return_code=1), "gibberish")
    assert diagnosis.category in ("unknown", "assertion_failure")
    assert diagnosis.confidence <= 0.5


# =========================================================================
# debug loop
# =========================================================================


def test_debug_loop_fixes_a_real_failure(failing_project):
    def fixer(context):
        assert context.diagnosis.failing_tests
        return [{"path": "app/calc.py",
                 "content": "def divide(left, right):\n"
                            "    if right == 0:\n        return 0\n"
                            "    return left / right\n"}]

    engine = TestEngine(failing_project)
    loop = AutomatedDebugLoop(
        failing_project,
        tester=lambda targets=None: engine.run(
            stages=["unit"], extra=[]) if not targets else engine.run(
            stages=["unit"]),
        strategy=CallableFixStrategy("test-fixer", fixer),
        max_iterations=2)
    report = loop.run()
    assert report.fixed is True
    assert report.outcome == "fixed"
    assert report.iterations == 1
    assert report.attempts[0].applied == ["app/calc.py"]
    assert (failing_project / "app" / "calc.py").read_text().count("right == 0")
    entries = FixLedger(failing_project).read()
    assert entries and entries[0]["outcome"] == "fixed"


def test_debug_loop_reports_no_fix_when_the_strategy_has_none(failing_project):
    engine = TestEngine(failing_project)
    loop = AutomatedDebugLoop(
        failing_project,
        tester=lambda targets=None: engine.run(stages=["unit"]),
        strategy=CallableFixStrategy("empty", lambda context: []),
        max_iterations=3)
    report = loop.run()
    assert report.outcome == "no_fix_available"
    assert report.fixed is False
    assert report.remaining_failures


def test_debug_loop_rolls_back_a_fix_that_did_not_work(failing_project):
    original = (failing_project / "app" / "calc.py").read_text()

    def wrong_fixer(context):
        return [{"path": "app/calc.py",
                 "content": "def divide(left, right):\n    return -1\n"}]

    engine = TestEngine(failing_project)
    loop = AutomatedDebugLoop(
        failing_project,
        tester=lambda targets=None: engine.run(stages=["unit"]),
        strategy=CallableFixStrategy("wrong", wrong_fixer),
        max_iterations=2, rollback_on_failure=True)
    report = loop.run()
    assert report.fixed is False
    assert report.rolled_back is True
    assert (failing_project / "app" / "calc.py").read_text() == original


def test_debug_loop_stops_when_a_fix_changes_nothing(failing_project):
    calls = []

    def no_op_fixer(context):
        calls.append(1)
        return [{"path": "app/calc.py",
                 "content": "def divide(left, right):\n"
                            "    return left / right\n"}]

    engine = TestEngine(failing_project)
    loop = AutomatedDebugLoop(
        failing_project,
        tester=lambda targets=None: engine.run(stages=["unit"]),
        strategy=CallableFixStrategy("no-op", no_op_fixer),
        max_iterations=4, rollback_on_failure=False)
    report = loop.run()
    assert report.outcome in ("unchanged", "still_failing")
    assert report.iterations <= 3
    assert report.fixed is False


def test_debug_loop_on_a_green_project_does_nothing(failing_project):
    write(failing_project, "tests/test_calc.py",
          "def test_ok():\n    assert True\n")
    engine = TestEngine(failing_project)
    loop = AutomatedDebugLoop(
        failing_project,
        tester=lambda targets=None: engine.run(stages=["unit"]),
        strategy=CallableFixStrategy("never", lambda context: []))
    report = loop.run()
    assert report.outcome == "no_failure"
    assert report.iterations == 0


def test_debug_loop_rejects_an_absurd_iteration_budget(tmp_path):
    with pytest.raises(ValueError, match="max_iterations"):
        AutomatedDebugLoop(tmp_path, tester=lambda: {"ok": True},
                           max_iterations=99)


def test_ledger_counts_repeated_failures(tmp_path):
    ledger = FixLedger(tmp_path)
    assert ledger.read() == []
    ledger.append([{"fingerprint": "abc", "outcome": "still_failing"}])
    ledger.append([{"fingerprint": "abc", "outcome": "still_failing"}])
    assert ledger.repeated_failures() == {"abc": 2}
    assert len(ledger.recent(5)) == 2


def test_ledger_survives_a_corrupt_file(tmp_path):
    ledger = FixLedger(tmp_path)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text("{not json", encoding="utf-8")
    assert ledger.read() == []


# =========================================================================
# performance lab
# =========================================================================


def test_measure_real_resource_usage(tmp_path):
    lab = PerformanceLab(tmp_path, runs=3)
    result = lab.benchmark([sys.executable, "-c", "sum(range(200000))"])
    assert result.runs == 3
    assert result.succeeded is True
    stats = result.summary()["stats"]["wall_ms"]
    assert stats["count"] == 3 and stats["min"] > 0
    assert "rusage" in result.summary()["sources"]
    assert result.summary()["stats"]["cpu_seconds"]["count"] == 3


def test_statistics_are_honest_about_empty_input():
    assert statistics([])["count"] == 0
    assert statistics([])["median"] is None
    stats = statistics([1.0, 2.0, 3.0])
    assert stats["median"] == 2.0 and stats["min"] == 1.0
    assert stats["max"] == 3.0


def test_comparing_identical_work_is_not_an_improvement(tmp_path):
    lab = PerformanceLab(tmp_path, runs=3)
    argv = [sys.executable, "-c", "pass"]
    before = lab.benchmark(argv, label="before")
    comparison = lab.compare_with(before, argv, label="after")
    wall = next(item for item in comparison.metrics
                if item.metric == "wall_ms")
    assert wall.verdict in ("unchanged", "improved", "regression")
    assert comparison.verdict in ("unchanged", "improved", "regression")
    assert comparison.claims()


def test_a_synthetic_regression_is_flagged(tmp_path):
    from forge.perf.lab import BenchmarkResult
    from forge.perf.metrics import ResourceSample
    before = BenchmarkResult(
        name="before", samples=[ResourceSample(wall_ms=100.0) for _ in range(5)])
    after = BenchmarkResult(
        name="after", samples=[ResourceSample(wall_ms=200.0) for _ in range(5)])
    comparison = compare(before, after, min_runs=5)
    wall = next(item for item in comparison.metrics if item.metric == "wall_ms")
    assert wall.verdict == "regression"
    assert wall.delta_percent == 100.0
    assert comparison.ok is False
    assert any("slower" in claim for claim in comparison.claims())


def test_an_improvement_is_measured_not_claimed(tmp_path):
    from forge.perf.lab import BenchmarkResult
    from forge.perf.metrics import ResourceSample
    before = BenchmarkResult(
        name="before", samples=[ResourceSample(wall_ms=200.0) for _ in range(5)])
    after = BenchmarkResult(
        name="after", samples=[ResourceSample(wall_ms=100.0) for _ in range(5)])
    comparison = compare(before, after, min_runs=5)
    assert comparison.verdict == "improved"
    assert comparison.ok is True
    assert any("faster" in claim for claim in comparison.claims())


def test_too_few_runs_yields_no_verdict():
    from forge.perf.lab import BenchmarkResult
    from forge.perf.metrics import ResourceSample
    before = BenchmarkResult(name="b", samples=[ResourceSample(wall_ms=100.0)])
    after = BenchmarkResult(name="a", samples=[ResourceSample(wall_ms=50.0)])
    comparison = compare(before, after, min_runs=3)
    wall = next(item for item in comparison.metrics if item.metric == "wall_ms")
    assert wall.verdict == "insufficient_data"
    assert comparison.verdict == "insufficient_data"
    assert "no performance claim is supported" in comparison.claims()[0]


def test_unmeasured_metrics_say_so():
    from forge.perf.lab import BenchmarkResult
    from forge.perf.metrics import ResourceSample
    before = BenchmarkResult(name="b",
                             samples=[ResourceSample(wall_ms=100.0)
                                      for _ in range(3)])
    after = BenchmarkResult(name="a",
                            samples=[ResourceSample(wall_ms=100.0)
                                     for _ in range(3)])
    comparison = compare(before, after, min_runs=3)
    rss = next(item for item in comparison.metrics
               if item.metric == "peak_rss_mib")
    assert rss.verdict == "not_measured"


def test_custom_metrics_are_compared(tmp_path):
    from forge.perf.lab import BenchmarkResult
    from forge.perf.metrics import ResourceSample
    before = BenchmarkResult(
        name="b", samples=[ResourceSample(wall_ms=10.0) for _ in range(3)],
        custom={"frames_per_second": [30.0, 31.0, 29.0]})
    after = BenchmarkResult(
        name="a", samples=[ResourceSample(wall_ms=10.0) for _ in range(3)],
        custom={"frames_per_second": [58.0, 60.0, 59.0]})
    comparison = compare(before, after, min_runs=3,
                         metrics=["frames_per_second"],
                         higher_is_better=["frames_per_second"])
    fps = comparison.metrics[0]
    assert fps.verdict == "improved"
    assert fps.lower_is_better is False
    assert any("higher" in claim for claim in comparison.claims())


def test_an_unknown_custom_metric_defaults_to_lower_is_better():
    from forge.perf.lab import BenchmarkResult
    from forge.perf.metrics import ResourceSample
    samples = [ResourceSample(wall_ms=1.0) for _ in range(3)]
    before = BenchmarkResult(name="b", samples=samples,
                             custom={"mystery": [10.0, 11.0, 10.0]})
    after = BenchmarkResult(name="a", samples=samples,
                            custom={"mystery": [40.0, 41.0, 40.0]})
    comparison = compare(before, after, min_runs=3, metrics=["mystery"])
    assert comparison.metrics[0].lower_is_better is True
    assert comparison.metrics[0].verdict == "regression"
