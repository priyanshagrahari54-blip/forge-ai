"""Agent Creation Engine (A81): benchmark testing.

Benchmarks are deterministic, offline, code-judged scenarios that gate
the ``tested`` lifecycle state. These tests pin the harness, the
scenario set, the requirement gate, and the honest demotion on failure.
"""
from __future__ import annotations

import json

import pytest

from forge.agent_engine.benchmarks import (
    BENCHMARK_IDS,
    AgentBenchmarkHarness,
)
from forge.agent_engine.errors import AgentEngineError, LifecycleError
from forge.agent_engine.manager import AgentManager
from forge.agent_engine.spec import AgentSpec
from forge.agent_engine.templates import build_template_spec


def test_benchmark_ids_cover_templates():
    assert set(BENCHMARK_IDS) == {
        "generic", "coding", "research", "security", "game-dev",
        "os-dev", "documentation"}


def test_unknown_benchmark_refused(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    manager.create("coder-1", template="coding")
    runtime = manager.runtime_for("coder-1", enabled=True,
                                  benchmark_rig=True)
    with pytest.raises(AgentEngineError):
        AgentBenchmarkHarness().run("bogus", runtime, tmp_path)


def test_benchmark_run_is_deterministic_and_offline(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    manager.create("coder-1", template="coding")
    runtime = manager.runtime_for("coder-1", enabled=True,
                                  benchmark_rig=True,
                                  approver=lambda *a: True)
    first = AgentBenchmarkHarness().run("coding", runtime, tmp_path)
    second = AgentBenchmarkHarness().run("coding", runtime, tmp_path)
    assert first.total == second.total
    assert first.passed == second.passed
    assert first.score == second.score
    # No live model is ever contacted: the harness installs a recorded
    # stand-in fabric and every model request goes to it.
    assert runtime._fabric is not None
    assert runtime._fabric.requests
    assert runtime._fabric.requests[0]["capability"] == "coding"
    model = runtime.call_model("another probe")
    assert model["model"] == "benchmark-standin"
    assert model["success"] is True


def test_benchmark_scenarios_exercise_every_service(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    manager.create("coder-1", template="coding")
    runtime = manager.runtime_for("coder-1", enabled=True,
                                  benchmark_rig=True)
    harness = AgentBenchmarkHarness()
    scenario_ids = [scenario.id for scenario in harness.build("coding")]
    # Isolation boundaries + platform services, by id.
    for required in ("spec-integrity", "memory-scope", "tool-boundary",
                     "permission-boundary", "model-routing",
                     "checkpoint-rollback", "resource-accounting"):
        assert required in scenario_ids, required


def test_test_gate_moves_to_tested(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    manager.create("coder-1", template="coding")
    result = manager.test("coder-1")
    assert result["current_lifecycle"] == "tested"
    stored = manager.store.load_benchmarks("coder-1", 1)
    assert stored["benchmark"] == "coding"
    assert stored["score"] == 1.0
    assert stored["passed"] == stored["total"]


def test_failing_benchmark_demotes_honestly(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    spec = build_template_spec("coding", "coder-1").to_dict()
    # An unreachable requirement: more scenarios than the harness has.
    spec["verification"]["min_scenarios"] = 1000
    manager.create("coder-1", template="coding", overrides=spec)
    result = manager.test("coder-1")
    assert result["current_lifecycle"] == "validated"
    assert result["benchmark"]["meets_requirements"] is False
    assert "required" in result["benchmark"]["meets_detail"]
    # The result was still recorded honestly on the package.
    stored = manager.store.load_benchmarks("coder-1", 1)
    assert stored["score"] < 2.0
    assert stored["passed"] == stored["total"]  # scenarios all passed
    # And the agent still cannot be enabled.
    with pytest.raises(LifecycleError):
        manager.enable("coder-1")


def test_test_refuses_enabled_agents(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    manager.create("coder-1", template="coding")
    manager.test("coder-1")
    manager.enable("coder-1")
    with pytest.raises(LifecycleError):
        manager.test("coder-1")


def test_meets_requirements_scores(tmp_path):
    harness = AgentBenchmarkHarness()
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    manager.create("coder-1", template="coding")
    runtime = manager.runtime_for("coder-1", enabled=True,
                                  benchmark_rig=True,
                                  approver=lambda *a: True)
    result = harness.run("coding", runtime, tmp_path)
    verification = runtime.spec.verification
    meets, detail = harness.meets_requirements(result, verification)
    assert meets, detail
    # An unreachable scenario floor must fail.
    strict = AgentSpec.from_dict({
        **runtime.spec.to_dict(),
        "verification": {"benchmark": "coding", "min_score": 0.75,
                         "min_scenarios": 1000, "tests_required": True,
                         "security_review": False},
    })
    meets, detail = harness.meets_requirements(result, strict.verification)
    assert not meets
    assert "required" in detail


def test_benchmark_results_are_json_serializable(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    manager.create("coder-1", template="coding")
    result = manager.test("coder-1")
    json.dumps(result["benchmark"])  # must not raise
