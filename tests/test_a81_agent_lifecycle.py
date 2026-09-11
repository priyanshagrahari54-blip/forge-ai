"""A81: lifecycle states, gating, and version derivation."""
from __future__ import annotations

import pytest

from forge.agent_engine.engine import AgentCreationEngine, EngineError
from forge.agent_engine.lifecycle import (
    CREATED,
    DISABLED,
    ENABLED,
    PAUSED,
    RETIRED,
    TESTED,
    VALIDATED,
    LifecycleError,
    LifecycleManager,
)
from forge.agent_engine.version import (
    MAJOR,
    MINOR,
    PATCH,
    AgentVersion,
    VersionError,
    classify_change,
)
from forge.agent_engine.templates import from_template


@pytest.fixture()
def engine():
    return AgentCreationEngine()


def test_state_machine_allows_only_declared_transitions():
    manager = LifecycleManager()
    manager.register("a")
    assert manager.state("a") == CREATED
    manager.transition("a", VALIDATED, actor="operator")
    manager.transition("a", TESTED, actor="operator")
    manager.transition("a", ENABLED, actor="operator")
    manager.transition("a", PAUSED, actor="operator")
    manager.transition("a", ENABLED, actor="operator")
    manager.transition("a", DISABLED, actor="operator")
    with pytest.raises(LifecycleError):
        manager.transition("a", ENABLED, actor="operator")
    manager.transition("a", RETIRED, actor="operator")


def test_retirement_is_terminal():
    manager = LifecycleManager()
    manager.register("a", ENABLED)
    manager.transition("a", RETIRED, actor="operator")
    for target in (ENABLED, PAUSED, VALIDATED, DISABLED, CREATED):
        with pytest.raises(LifecycleError):
            manager.transition("a", target, actor="operator")


def test_an_agent_cannot_change_its_own_state():
    manager = LifecycleManager()
    manager.register("scout", TESTED)
    with pytest.raises(LifecycleError):
        manager.transition("scout", ENABLED, actor="scout")
    with pytest.raises(LifecycleError):
        manager.transition("scout", ENABLED, actor="agent:scout")
    manager.transition("scout", ENABLED, actor="operator")
    assert manager.state("scout") == ENABLED


def test_history_records_every_transition():
    manager = LifecycleManager()
    manager.register("a")
    manager.transition("a", VALIDATED, actor="operator", reason="ok")
    history = manager.history("a")
    assert history[-1]["from"] == CREATED
    assert history[-1]["to"] == VALIDATED
    assert history[-1]["actor"] == "operator"


def test_engine_full_happy_path(engine):
    package = engine.create(template="documentation")
    assert package.state == CREATED
    assert engine.validate(package.name)["valid"]
    report = engine.test(package.name, include_behavioural=False)
    assert report["passed"], report["failures"]
    assert engine.enable(package.name)["state"] == ENABLED
    assert engine.lifecycle.can_run(package.name)


def test_untested_agent_cannot_be_enabled(engine):
    package = engine.create(template="research")
    engine.validate(package.name)
    with pytest.raises(EngineError):
        engine.enable(package.name)


def test_disabled_agent_must_be_revalidated_before_enabling(engine):
    package = engine.create(template="security")
    engine.validate(package.name)
    engine.test(package.name, include_behavioural=False)
    engine.enable(package.name)
    engine.disable(package.name)
    with pytest.raises(EngineError):
        engine.enable(package.name)
    engine.validate(package.name)
    engine.test(package.name, include_behavioural=False)
    assert engine.enable(package.name)["state"] == ENABLED


def test_pause_and_resume_do_not_require_retesting(engine):
    package = engine.create(template="documentation")
    engine.validate(package.name)
    engine.test(package.name, include_behavioural=False)
    engine.enable(package.name)
    engine.pause(package.name)
    assert engine.get(package.name).state == PAUSED
    assert engine.enable(package.name)["state"] == ENABLED


def test_version_bumps_are_derived_not_declared():
    base = from_template("documentation")
    widened = base.with_changes(
        permissions=dict(base.permissions.to_dict(), allow_network=True))
    assert classify_change(base, widened) == MAJOR

    cosmetic = base.with_changes(purpose="Slightly different wording here.")
    assert classify_change(base, cosmetic) == PATCH

    narrowed = base.with_changes(
        resource_limits=dict(base.resource_limits.to_dict(),
                             max_runs_per_hour=5))
    assert classify_change(base, narrowed) == MINOR


@pytest.mark.parametrize("change", [
    {"permissions_flag": "allow_terminal"},
    {"permissions_flag": "allow_git_commit"},
])
def test_any_widening_is_major(change):
    base = from_template("documentation")
    new = base.with_changes(permissions=dict(
        base.permissions.to_dict(), **{change["permissions_flag"]: True}))
    assert classify_change(base, new) == MAJOR


def test_dropping_a_verification_gate_is_major():
    base = from_template("gamedev")
    weaker = base.with_changes(verification={
        "required_gates": ["security"], "require_checkpoint": True,
        "require_independent_review": True, "max_repair_attempts": 3})
    assert classify_change(base, weaker) == MAJOR


def test_revision_resets_the_lifecycle(engine):
    package = engine.create(template="documentation")
    engine.validate(package.name)
    engine.test(package.name, include_behavioural=False)
    engine.enable(package.name)
    revised = engine.revise(package.name, {
        "permissions": dict(package.spec.permissions.to_dict(),
                            allow_network=True)})
    assert str(revised.version) == "2.0.0"
    assert engine.get(package.name).state == CREATED
    assert not engine.lifecycle.can_run(package.name)
    with pytest.raises(EngineError):
        engine.enable(package.name)


def test_version_history_and_rollback(engine):
    package = engine.create(template="documentation")
    engine.revise(package.name, {"purpose": "Only the wording changed."})
    versions = engine.versions(package.name)
    assert [entry["version"] for entry in versions] == ["1.0.0", "1.0.1"]
    restored = engine.rollback_to(package.name, "1.0.0")
    assert str(restored.version) == "1.0.0"
    assert engine.get(package.name).state == CREATED


def test_version_parsing_is_strict():
    assert str(AgentVersion.parse("2.3.4")) == "2.3.4"
    for bad in ("", "1", "1.2", "x.y.z", "1.2.3.4"):
        with pytest.raises(VersionError):
            AgentVersion.parse(bad)


def test_renaming_across_versions_is_refused():
    base = from_template("documentation")
    with pytest.raises(VersionError):
        classify_change(base, base.with_changes(name="other-agent"))
