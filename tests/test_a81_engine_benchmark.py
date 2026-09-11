"""Agent Creation Engine (A81): benchmark testing.

The benchmark is the gate between ``validated`` and ``tested``. Every
check is judged by deterministic code on real engine objects — an agent
never grades itself, and failures are reported as failures.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from forge.agents.engine import (  # noqa: E402
    AgentCreationFactory,
    EngineBundle,
    EngineRuntime,
    run_agent_benchmark,
    spec_from_template,
)
from forge.agents.engine.benchmark import CHECK_ORDER  # noqa: E402
from helpers_a81 import make_engine, make_repo  # noqa: E402

EXPECTED_CHECKS = {
    "spec-validation", "lifecycle-gating", "tool-boundary",
    "permission-ceiling", "policy-gate", "self-grant-prohibition",
    "memory-isolation", "model-routing", "resource-limits",
}


def test_benchmark_passes_a_sound_agent(tmp_path):
    make_repo(tmp_path)
    engine = make_engine(tmp_path)
    factory = engine["factory"]
    package = factory.create(spec_from_template("coding", name="solid"),
                             created_by="alice")
    package = factory.validate("solid", actor="alice")
    benchmark = run_agent_benchmark(package, engine["runtime"], factory)
    assert benchmark["passed"] is True
    assert benchmark["passed_checks"] == benchmark["total_checks"] == 9
    assert {check["check"] for check in benchmark["checks"]} == \
        EXPECTED_CHECKS
    assert [check["check"] for check in benchmark["checks"]] == \
        list(CHECK_ORDER)
    assert benchmark["judged_by"] == "code"
    assert benchmark["agent"] == "solid"
    assert benchmark["version"] == 1


def test_benchmark_fails_honestly_without_a_model(tmp_path):
    """An agent whose model needs cannot be routed fails the benchmark."""
    make_repo(tmp_path)
    from forge.models.fabric import ModelFabric

    factory = AgentCreationFactory(root=str(tmp_path))
    # No provider anywhere can serve "vision" offline.
    package = factory.create(spec_from_template(
        "research", name="seer", overrides={
            "model_requirements": {"capabilities": ["vision"]}}),
        created_by="alice")
    bundle = EngineBundle.build(tmp_path,
                                fabric=ModelFabric.from_defaults())
    runtime = EngineRuntime(bundle)
    benchmark = run_agent_benchmark(package, runtime, factory)
    assert benchmark["passed"] is False
    routing = [check for check in benchmark["checks"]
               if check["check"] == "model-routing"]
    assert routing and routing[0]["passed"] is False
    assert "no model" in routing[0]["details"].lower()


def test_benchmark_fails_a_broken_spec(tmp_path):
    make_repo(tmp_path)
    engine = make_engine(tmp_path)
    factory = engine["factory"]
    # Corrupt the stored package so re-validation fails.
    factory.create(spec_from_template("coding", name="broken"),
                   created_by="alice")
    package = factory.get("broken")
    # Simulate a spec that no longer validates (defensive re-check).
    package.spec = spec_from_template("coding", name="broken",
                                      purpose="p")
    object.__setattr__(package.spec, "capabilities", ("bogus-cap",))
    benchmark = run_agent_benchmark(package, engine["runtime"], factory)
    assert benchmark["passed"] is False
    spec_check = [check for check in benchmark["checks"]
                  if check["check"] == "spec-validation"]
    assert spec_check and spec_check[0]["passed"] is False


def test_benchmark_refuses_to_grade_an_enabled_agent_lifecycle(tmp_path):
    """The lifecycle probe needs a non-enabled agent to refuse a run."""
    make_repo(tmp_path)
    engine = make_engine(tmp_path)
    factory = engine["factory"]
    package = factory.create(spec_from_template("coding", name="live"),
                             created_by="alice")
    factory.validate("live", actor="alice")
    benchmark = run_agent_benchmark(package, engine["runtime"], factory)
    factory.mark_tested("live", actor="alice", benchmark=benchmark)
    factory.enable("live", actor="alice")
    recheck = run_agent_benchmark(factory.get("live"),
                                  engine["runtime"], factory)
    assert recheck["passed"] is False
    gating = [check for check in recheck["checks"]
              if check["check"] == "lifecycle-gating"]
    assert gating and gating[0]["passed"] is False
    assert "already enabled" in gating[0]["details"]


def test_benchmark_memory_isolation_with_shared_pool(tmp_path):
    """Shared memory is opt-in; isolation checks still pass either way."""
    make_repo(tmp_path)
    engine = make_engine(tmp_path)
    factory = engine["factory"]
    sharer = factory.create(spec_from_template(
        "coding", name="sharer", overrides={
            "memory_policy": {"share_across_agents": True}}),
        created_by="alice")
    benchmark = run_agent_benchmark(sharer, engine["runtime"], factory)
    isolation = [check for check in benchmark["checks"]
                 if check["check"] == "memory-isolation"]
    assert isolation and isolation[0]["passed"] is True
    # The shared pool is a distinct namespace from private ones.
    store = engine["runtime"]._memory_store(sharer)
    assert Path(store.root).name == "memory"
    assert "_shared" in str(store.root)


def test_benchmark_survives_disabled_memory(tmp_path):
    make_repo(tmp_path)
    engine = make_engine(tmp_path)
    factory = engine["factory"]
    quiet = factory.create(spec_from_template(
        "research", name="quiet", overrides={
            "memory_policy": {"enabled": False}}), created_by="alice")
    benchmark = run_agent_benchmark(quiet, engine["runtime"], factory)
    isolation = [check for check in benchmark["checks"]
                 if check["check"] == "memory-isolation"]
    assert isolation and isolation[0]["passed"] is True
    assert "disabled" in isolation[0]["details"]
