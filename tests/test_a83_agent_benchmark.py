"""A83 — agent benchmark testing.

The benchmark must be honest above all: a scenario that could not run is
``skipped``, never passed; a required scenario cannot be skipped; and the
report explains itself. These tests also pin that the boundary scenarios
exercise the real classes rather than restating their logic.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a83 import make_engine, make_project  # noqa: E402

from forge.agents.engine import (  # noqa: E402
    REQUIRED_SCENARIOS,
    AgentState,
    BenchmarkReport,
    ScenarioResult,
    run_agent_benchmark,
)
from forge.agents.engine.benchmark import (  # noqa: E402
    FAILED,
    PASSED,
    SKIPPED,
    _scenario_lifecycle_gate,
    _scenario_resource_limits,
    _scenario_self_grant,
)


@pytest.fixture()
def engine(tmp_path):
    make_project(tmp_path / "demo")
    return make_engine(tmp_path / "demo")


def _validated(engine, name="worker", template="coding", **kwargs):
    engine.create_from_template(template, name, actor="alice", **kwargs)
    report = engine.validate(name, actor="alice")
    assert report["passed"], report
    return engine.get(name)


# -- report honesty ------------------------------------------------------


def test_a_report_with_nothing_executed_never_passes():
    report = BenchmarkReport(agent="x", version="1.0.0", run_id="r1")
    assert report.executed == 0
    assert report.passed is False
    assert report.reason() == "no scenario executed"


def test_skipped_scenarios_are_never_counted_as_passed():
    report = BenchmarkReport(agent="x", version="1.0.0", run_id="r1")
    report.scenarios = [ScenarioResult("a", PASSED),
                        ScenarioResult("b", SKIPPED),
                        ScenarioResult("c", SKIPPED)]
    assert report.executed == 1
    assert report.skipped_count == 2
    assert report.pass_rate == 1.0
    # Required scenarios were skipped, so the report still fails.
    assert report.passed is False
    assert "required scenario(s) not passed" in report.reason()


def test_one_failure_fails_the_whole_report():
    report = BenchmarkReport(agent="x", version="1.0.0", run_id="r1")
    report.scenarios = [
        ScenarioResult(name, PASSED) for name in REQUIRED_SCENARIOS]
    report.scenarios.append(ScenarioResult("extra", FAILED))
    assert report.passed is False
    assert "pass rate" in report.reason()
    assert "1 of 8" in report.reason()


def test_pass_rate_threshold_governs_optional_scenarios():
    required = REQUIRED_SCENARIOS[:6]
    optional = ("model-route", "answer-quality")

    def build(min_rate):
        report = BenchmarkReport(agent="x", version="1.0.0", run_id="r1",
                                 required=required, min_pass_rate=min_rate)
        report.scenarios = [ScenarioResult(name, PASSED)
                            for name in required]
        report.scenarios.append(ScenarioResult(optional[0], FAILED))
        report.scenarios.append(ScenarioResult(optional[1], PASSED))
        return report

    assert build(0.9).pass_rate == pytest.approx(7 / 8)
    assert build(0.9).passed is False
    assert "pass rate" in build(0.9).reason()
    assert build(0.8).passed is True
    assert "optional scenario(s) failed" in build(0.8).reason()


def test_a_failed_required_scenario_fails_at_any_rate():
    names = list(REQUIRED_SCENARIOS)
    report = BenchmarkReport(agent="x", version="1.0.0", run_id="r1",
                             required=tuple(names), min_pass_rate=0.1)
    report.scenarios = [ScenarioResult(names[0], FAILED)]
    report.scenarios += [ScenarioResult(name, PASSED) for name in names[1:]]
    assert report.passed is False
    assert names[0] in report.missing_required()


def test_report_serialisation_keeps_counts_and_verdict_separate():
    report = BenchmarkReport(agent="x", version="1.0.0", run_id="r1")
    report.scenarios = [ScenarioResult("a", PASSED),
                        ScenarioResult("b", FAILED),
                        ScenarioResult("c", SKIPPED)]
    payload = report.to_dict()
    assert payload["executed"] == 2
    assert payload["passed_scenarios"] == 1
    assert payload["failed_scenarios"] == 1
    assert payload["skipped_scenarios"] == 1
    assert payload["passed"] is False
    assert payload["pass_rate"] == 0.5


# -- the real suite ------------------------------------------------------


def test_boundary_scenarios_run_without_a_model(engine):
    package = _validated(engine)
    report = run_agent_benchmark(engine.runtime, package,
                                 include_model_checks=False)
    assert report.executed == len(REQUIRED_SCENARIOS)
    assert report.skipped_count == 0
    assert report.passed is True, report.to_dict()
    assert {item.name for item in report.scenarios} == set(
        REQUIRED_SCENARIOS)


def test_model_scenarios_are_skipped_honestly_without_a_model(tmp_path):
    from forge.agents.engine import AgentCreationEngine

    make_project(tmp_path / "demo")
    offline = AgentCreationEngine(str(tmp_path / "demo"))
    offline.create_from_template("coding", "worker", actor="alice")
    offline.validate("worker", actor="alice")
    report = run_agent_benchmark(offline.runtime, offline.get("worker"))
    by_name = {item.name: item for item in report.scenarios}
    assert by_name["model-route"].status == SKIPPED
    assert by_name["answer-quality"].status == SKIPPED
    assert "no real model" in by_name["answer-quality"].detail
    assert report.model_available is False
    assert report.passed is True, \
        "skipped model checks must not fail an otherwise sound agent"


def test_model_scenarios_execute_with_a_real_model(engine):
    package = _validated(engine)
    report = run_agent_benchmark(engine.runtime, package)
    by_name = {item.name: item for item in report.scenarios}
    assert report.model_available is True
    assert by_name["model-route"].status == PASSED, by_name["model-route"]
    assert by_name["answer-quality"].status == PASSED
    assert report.executed == len(REQUIRED_SCENARIOS) + 2


def test_spec_integrity_detects_a_tampered_package(engine):
    import json

    package = _validated(engine)
    manifest = package.directory / "agent.json"
    payload = json.loads(manifest.read_text("utf-8"))
    payload["spec"]["purpose"] = "Quietly rewritten after validation."
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    report = run_agent_benchmark(engine.runtime, engine.get("worker"),
                                 include_model_checks=False)
    by_name = {item.name: item for item in report.scenarios}
    assert by_name["spec-integrity"].status == FAILED
    assert report.passed is False


def test_tool_boundary_is_skipped_only_for_a_full_catalog_spec(engine):
    from forge.agents.engine import spec_from_template
    from forge.agents.engine.benchmark import _scenario_tool_boundary

    package = _validated(engine)
    assert _scenario_tool_boundary(engine.runtime, package).status == PASSED

    everything = spec_from_template(
        "coding", name="omni",
        overrides={
            "tools": [{"name": name} for name in
                      ("read_file", "search", "git_status", "run_tests",
                       "write_file", "delete_file", "terminal")],
            "permissions": {
                "operations": ["read_file", "search_files", "git_status",
                               "run_tests", "write_file", "delete_file",
                               "run_command"],
                "mode_ceiling": "assisted"}})
    engine.factory.create(everything, actor="alice")
    result = _scenario_tool_boundary(engine.runtime, engine.get("omni"))
    assert result.status == SKIPPED
    assert "whole tool catalog" in result.detail


def test_permission_boundary_covers_every_forbidden_shape(engine, tmp_path):
    package = _validated(engine)
    project = Path(engine.root)
    before = sorted(str(p) for p in project.rglob("*") if p.is_file())
    report = run_agent_benchmark(engine.runtime, package,
                                 include_model_checks=False)
    by_name = {item.name: item for item in report.scenarios}
    assert by_name["permission-boundary"].status == PASSED
    assert "refused" in by_name["permission-boundary"].detail
    after = sorted(str(p) for p in project.rglob("*") if p.is_file())
    assert before == after, "a benchmark probe must not write any file"


def test_memory_isolation_leaves_no_residue(engine):
    package = _validated(engine)
    report = run_agent_benchmark(engine.runtime, package,
                                 include_model_checks=False)
    by_name = {item.name: item for item in report.scenarios}
    assert by_name["memory-isolation"].status == PASSED
    memory_root = Path(engine.root) / ".forge" / "memory"
    leftovers = [p for p in memory_root.rglob("*")
                 if p.is_file() and "probe-key" in p.read_text("utf-8")]
    assert leftovers == []


def test_self_grant_scenario_records_the_refusal():
    from forge.agents.engine import spec_from_template

    spec = spec_from_template("coding", name="exporter")
    result = _scenario_self_grant(
        type("P", (), {"name": spec.name, "spec": spec})())
    assert result.status == PASSED
    assert "recorded" in result.detail


def test_resource_limit_scenario_uses_real_budgets():
    from forge.agents.engine import ResourceLimits

    result = _scenario_resource_limits(ResourceLimits())
    assert result.status == PASSED
    assert "wall clock" in result.detail


def test_lifecycle_scenario_covers_every_state():
    from forge.agents.engine import spec_from_template, LifecycleRecord

    spec = spec_from_template("coding", name="exporter")
    package = type("P", (), {"name": "exporter", "spec": spec,
                             "directory": Path("."), "version": "1.0.0",
                             "lifecycle": LifecycleRecord(),
                             "created_by": "alice", "created_at": 0.0})()
    result = _scenario_lifecycle_gate(package)
    assert result.status == PASSED
    assert "enabled" in result.detail


# -- the lifecycle gate it drives ---------------------------------------


def test_test_records_the_report_on_the_package(engine):
    _validated(engine)
    result = engine.test("worker", actor="alice")
    assert result["passed"] is True
    stored = engine.benchmarks("worker")
    assert len(stored) == 1
    assert stored[0]["run_id"] == result["run_id"]
    assert engine.get("worker").state == AgentState.TESTED
    assert engine.get("worker").lifecycle.benchmark["passed"] is True


def test_retesting_records_a_second_report(engine):
    _validated(engine)
    engine.test("worker", actor="alice")
    engine.test("worker", actor="alice")
    assert len(engine.benchmarks("worker")) == 2


def test_required_scenarios_come_from_the_spec(engine):
    _validated(engine, overrides={
        "verification": {"require_tests": True,
                         "required_scenarios": ["spec-integrity",
                                                "lifecycle-gate"]}})
    result = engine.test("worker", actor="alice",
                         include_model_checks=False)
    assert result["required"] == ["spec-integrity", "lifecycle-gate"]
    assert result["passed"] is True


def test_a_required_scenario_that_is_absent_fails_the_report(engine):
    _validated(engine, overrides={
        "verification": {"require_tests": True,
                         "required_scenarios": ["spec-integrity",
                                                "not-a-real-scenario"]}})
    result = engine.test("worker", actor="alice",
                         include_model_checks=False)
    assert result["passed"] is False
    assert "not-a-real-scenario" in result["missing_required"]
    assert engine.get("worker").state == AgentState.VALIDATED
