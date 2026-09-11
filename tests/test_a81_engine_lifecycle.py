"""Agent Creation Engine (A81): lifecycle, factory, versioning.

The lifecycle is created → validated → tested → enabled ⇄ paused →
disabled → retired (terminal). Only the factory can move a package,
only operators can drive it, and every spec change becomes a new
immutable version that resets the lifecycle.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.agents.engine import (  # noqa: E402
    AgentCreationFactory,
    AgentPackage,
    LifecycleError,
    LifecycleState,
    PackageStore,
    transition,
)
from forge.agents.engine.templates import spec_from_template  # noqa: E402
from helpers_a81 import make_repo  # noqa: E402


def test_lifecycle_table_is_explicit():
    assert transition("created", "validated") is LifecycleState.VALIDATED
    assert transition("validated", "tested") is LifecycleState.TESTED
    assert transition("tested", "enabled") is LifecycleState.ENABLED
    assert transition("enabled", "paused") is LifecycleState.PAUSED
    assert transition("paused", "enabled") is LifecycleState.ENABLED
    assert transition("enabled", "disabled") is LifecycleState.DISABLED
    assert transition("paused", "disabled") is LifecycleState.DISABLED
    assert transition("disabled", "tested") is LifecycleState.TESTED
    assert transition("disabled", "retired") is LifecycleState.RETIRED
    # Refused shortcuts and reversals.
    for current, target in (("created", "enabled"),
                            ("created", "tested"),
                            ("tested", "paused"),
                            ("validated", "enabled"),
                            ("enabled", "created"),
                            ("disabled", "enabled")):
        with pytest.raises(LifecycleError):
            transition(current, target)
    with pytest.raises(LifecycleError):
        transition("enabled", "paused-up")
    with pytest.raises(LifecycleError):
        transition("flapping", "enabled")


def test_lifecycle_retired_is_terminal():
    with pytest.raises(LifecycleError, match="terminal"):
        transition("retired", "enabled")
    with pytest.raises(LifecycleError, match="terminal"):
        transition("retired", "retired")


def test_factory_create_emits_structured_package(tmp_path):
    make_repo(tmp_path)
    factory = AgentCreationFactory(root=str(tmp_path))
    spec = spec_from_template("coding", name="pkg-agent",
                              purpose="builds things")
    package = factory.create(spec, created_by="alice")
    assert package.name == "pkg-agent"
    assert package.status == "created"
    assert package.version == 1
    assert package.agent_id.startswith("agt_")
    manifest = package.manifest()
    assert manifest["format"] == "forge-agent-package"
    assert manifest["specification"]["purpose"] == "builds things"
    assert set(manifest["subsystems"]) == {
        "model_fabric", "policy_gate", "tool_runtime", "memory",
        "verification", "checkpoints"}
    assert manifest["version_history"][0]["version"] == 1
    # Duplicate names are refused.
    with pytest.raises(ValueError):
        factory.create(spec_from_template("coding", name="pkg-agent"),
                       created_by="alice")


def test_factory_full_lifecycle_walk(tmp_path):
    make_repo(tmp_path)
    factory = AgentCreationFactory(root=str(tmp_path))
    factory.create(spec_from_template("coding", name="walker"),
                   created_by="alice")
    package = factory.validate("walker", actor="alice")
    assert package.status == "validated"
    benchmark = {"passed": True, "passed_checks": 9, "total_checks": 9,
                 "checks": []}
    package = factory.mark_tested("walker", actor="alice",
                                  benchmark=benchmark)
    assert package.status == "tested"
    assert package.last_benchmark["passed"] is True
    package = factory.enable("walker", actor="alice")
    assert package.status == "enabled"
    package = factory.pause("walker", actor="alice")
    assert package.status == "paused"
    package = factory.resume("walker", actor="alice")
    assert package.status == "enabled"
    package = factory.disable("walker", actor="alice")
    assert package.status == "disabled"
    # Disabled agents must re-test before re-enabling.
    with pytest.raises(LifecycleError):
        factory.enable("walker", actor="alice")
    package = factory.mark_tested("walker", actor="alice",
                                  benchmark=benchmark)
    package = factory.enable("walker", actor="alice")
    assert package.status == "enabled"
    package = factory.retire("walker", actor="alice")
    assert package.status == "retired"
    with pytest.raises(LifecycleError):
        factory.resume("walker", actor="alice")


def test_factory_refuses_unvalidated_or_untested_advancement(tmp_path):
    make_repo(tmp_path)
    factory = AgentCreationFactory(root=str(tmp_path))
    factory.create(spec_from_template("coding", name="skipper"),
                   created_by="alice")
    # created -> enabled directly is refused.
    with pytest.raises(LifecycleError):
        factory.enable("skipper", actor="alice")
    factory.validate("skipper", actor="alice")
    # validated -> enabled without testing is refused.
    with pytest.raises(LifecycleError):
        factory.enable("skipper", actor="alice")
    # A failed benchmark cannot mark the agent tested.
    with pytest.raises(ValueError, match="did not pass"):
        factory.mark_tested("skipper", actor="alice",
                            benchmark={"passed": False})


def test_versioning_is_append_only_and_resets_lifecycle(tmp_path):
    make_repo(tmp_path)
    factory = AgentCreationFactory(root=str(tmp_path))
    factory.create(spec_from_template("coding", name="versioned",
                                      purpose="v1 purpose"),
                   created_by="alice")
    factory.validate("versioned", actor="alice")
    benchmark = {"passed": True, "passed_checks": 9, "total_checks": 9}
    factory.mark_tested("versioned", actor="alice", benchmark=benchmark)
    factory.enable("versioned", actor="alice")

    # Update: new version, lifecycle resets to created.
    v2 = factory.update(
        "versioned",
        spec_from_template("coding", name="versioned",
                           purpose="v2 purpose"),
        actor="alice", note="tighter tools")
    assert v2.version == 2
    assert v2.status == "created"
    assert v2.last_benchmark == {}
    assert [r.version for r in v2.history.versions()] == [1, 2]
    # v1 snapshot is immutable and still loadable.
    v1 = factory.get_version("versioned", 1)
    assert v1.spec.purpose == "v1 purpose"
    assert v1.version == 1
    # Names are immutable across versions.
    with pytest.raises(ValueError):
        factory.update("versioned",
                       spec_from_template("coding", name="renamed"),
                       actor="alice")
    # Rollback appends a NEW version rather than mutating history.
    v3 = factory.rollback("versioned", 1, actor="alice")
    assert v3.version == 3
    assert v3.spec.purpose == "v1 purpose"
    assert v3.status == "created"
    with pytest.raises(ValueError):
        factory.get_version("versioned", 99)


def test_package_store_roundtrip_and_bounds(tmp_path):
    make_repo(tmp_path)
    store = PackageStore(tmp_path)
    factory = AgentCreationFactory(store=store)
    spec = spec_from_template("research", name="stored-agent")
    package = factory.create(spec, created_by="bob")
    loaded = store.load("stored-agent")
    assert loaded.agent_id == package.agent_id
    assert loaded.spec == package.spec
    assert loaded.status == "created"
    assert store.names() == ["stored-agent"]
    # Traversal-style names are rejected by the store.
    with pytest.raises(ValueError):
        store.load("../escape")
    with pytest.raises(ValueError):
        store._agent_dir("..")
    # Corrupt manifests are refused honestly.
    (tmp_path / ".forge" / "agents" / "stored-agent" /
     "package.json").write_text("{not json")
    with pytest.raises(ValueError):
        store.load("stored-agent")
    # Unknown format is refused.
    bad = tmp_path / ".forge" / "agents" / "bad-agent"
    bad.mkdir(parents=True)
    (bad / "package.json").write_text('{"format": "other"}')
    with pytest.raises(ValueError):
        AgentPackage.from_manifest({"format": "other"})


def test_factory_delete_requires_retired(tmp_path):
    make_repo(tmp_path)
    factory = AgentCreationFactory(root=str(tmp_path))
    factory.create(spec_from_template("coding", name="doomed"),
                   created_by="alice")
    with pytest.raises(LifecycleError):
        factory.delete("doomed", actor="alice")
    factory.retire("doomed", actor="alice")
    removed = factory.delete("doomed", actor="alice")
    assert removed["agent_id"]
    assert factory.names() == []
    with pytest.raises(ValueError):
        factory.get("doomed")
