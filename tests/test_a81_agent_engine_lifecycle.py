"""Agent Creation Engine (A81): lifecycle, storage, and versioning.

The lifecycle is a centrally enforced state machine; the store keeps
immutable versioned packages. These tests pin both.
"""
from __future__ import annotations

import pytest

from forge.agent_engine.errors import (
    AgentNotFoundError,
    LifecycleError,
    NotRunnableError,
    SpecError,
)
from forge.agent_engine.lifecycle import (
    ALLOWED_TRANSITIONS,
    AgentLifecycle,
    can_transition,
    is_runnable,
)
from forge.agent_engine.manager import AgentManager
from forge.agent_engine.store import AgentManifest, AgentStore


# ---------------------------------------------------------------------------
# Lifecycle machine
# ---------------------------------------------------------------------------

def test_lifecycle_states_and_runnable():
    assert {state.value for state in AgentLifecycle} == {
        "created", "validated", "tested", "enabled", "paused",
        "disabled", "retired"}
    assert is_runnable("enabled")
    for state in ("created", "validated", "tested", "paused", "disabled",
                  "retired"):
        assert not is_runnable(state)


def test_lifecycle_happy_path_transitions():
    assert can_transition("created", "validated")
    assert can_transition("validated", "tested")
    assert can_transition("tested", "enabled")
    assert can_transition("enabled", "paused")
    assert can_transition("paused", "enabled")
    assert can_transition("enabled", "disabled")
    assert can_transition("disabled", "retired")
    assert can_transition("tested", "validated")   # honest demotion
    assert can_transition("disabled", "validated")  # re-entry path


def test_lifecycle_illegal_transitions_refused():
    from forge.agent_engine.lifecycle import require_transition

    for current, target in (
            ("created", "enabled"), ("validated", "enabled"),
            ("tested", "paused"), ("enabled", "tested"),
            ("enabled", "retired"), ("paused", "tested"),
            ("paused", "retired"), ("disabled", "enabled"),
            ("disabled", "tested"), ("retired", "enabled"),
            ("retired", "disabled")):
        assert not can_transition(current, target), (current, target)
        with pytest.raises(LifecycleError):
            require_transition(current, target)


def test_retired_is_terminal():
    assert ALLOWED_TRANSITIONS[AgentLifecycle.RETIRED] == frozenset()
    assert not can_transition("retired", "validated")


# ---------------------------------------------------------------------------
# Store: immutability, integrity, versioning
# ---------------------------------------------------------------------------

def test_store_versions_are_immutable(tmp_path):
    from forge.agent_engine.factory import AgentFactory
    from forge.agent_engine.spec import AgentSpec
    from forge.agent_engine.templates import build_template_spec

    store = AgentStore(tmp_path / "agents")
    factory = AgentFactory(store)
    spec = build_template_spec("coding", "coder-1")
    manifest = factory.create(spec)
    # Re-saving the same version must be refused.
    with pytest.raises(AgentNotFoundError):
        store.save_version(manifest)
    # The stored package round-trips identically.
    loaded = store.load_manifest("coder-1")
    assert loaded.to_dict() == manifest.to_dict()


def test_store_confines_agent_names(tmp_path):
    store = AgentStore(tmp_path / "agents")
    with pytest.raises(AgentNotFoundError):
        store.load_manifest("../escape")
    with pytest.raises(AgentNotFoundError):
        store.load_manifest("not a name")
    assert store.list_agents() == []


def test_store_integrity_detects_tampering(tmp_path):
    import json

    from forge.agent_engine.factory import AgentFactory
    from forge.agent_engine.templates import build_template_spec

    store = AgentStore(tmp_path / "agents")
    factory = AgentFactory(store)
    factory.create(build_template_spec("coding", "coder-1"))
    ok, detail = store.verify_integrity("coder-1")
    assert ok, detail
    # Tamper with the permissions lock.
    lock_path = tmp_path / "agents" / "coder-1" / "versions" / "v1" \
        / "permissions.lock"
    lock = json.loads(lock_path.read_text())
    lock["permissions"] = ["read_file", "delete_repository"]
    lock_path.write_text(json.dumps(lock))
    ok, detail = store.verify_integrity("coder-1")
    assert not ok
    assert "mismatch" in detail or "does not match" in detail


def test_store_history_is_append_only(tmp_path):
    from forge.agent_engine.factory import AgentFactory
    from forge.agent_engine.templates import build_template_spec

    store = AgentStore(tmp_path / "agents")
    factory = AgentFactory(store)
    factory.create(build_template_spec("coding", "coder-1"))
    first = store.history("coder-1")
    assert [event["event"] for event in first] == ["created"]
    store.append_event("coder-1", {"event": "probe", "k": "v"})
    second = store.history("coder-1")
    assert [event["event"] for event in second] == ["created", "probe"]
    assert second[0] == first[0]  # nothing was rewritten


# ---------------------------------------------------------------------------
# Manager: full lifecycle walk
# ---------------------------------------------------------------------------

def test_manager_full_lifecycle(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    created = manager.create("coder-1", template="coding")
    assert created["current_lifecycle"] == "validated"

    # Only tested agents may be enabled.
    with pytest.raises(LifecycleError):
        manager.enable("coder-1")

    tested = manager.test("coder-1")
    assert tested["current_lifecycle"] == "tested"
    assert tested["benchmark"]["score"] >= tested["agent"]["spec"][
        "verification"]["min_score"]

    enabled = manager.enable("coder-1")
    assert enabled["current_lifecycle"] == "enabled"
    assert enabled["runnable"] is True

    paused = manager.pause("coder-1")
    assert paused["current_lifecycle"] == "paused"
    assert manager.resume("coder-1")["current_lifecycle"] == "enabled"

    disabled = manager.disable("coder-1")
    assert disabled["current_lifecycle"] == "disabled"

    # Re-enabling from disabled is forbidden: validate -> test -> enable.
    with pytest.raises(LifecycleError):
        manager.enable("coder-1")
    manager.validate("coder-1")
    manager.test("coder-1")
    assert manager.enable("coder-1")["current_lifecycle"] == "enabled"

    manager.disable("coder-1")
    assert manager.retire("coder-1")["current_lifecycle"] == "retired"
    with pytest.raises(LifecycleError):
        manager.enable("coder-1")
    with pytest.raises(LifecycleError):
        manager.validate("coder-1")


def test_manager_only_latest_version_may_be_enabled(tmp_path):
    from forge.agent_engine.templates import build_template_spec

    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    manager.create("coder-1", template="coding")
    manager.test("coder-1")
    manager.enable("coder-1")
    # Publish v2 (same permissions) - v1 must no longer be enableable.
    v2 = manager.create_version(
        "coder-1", build_template_spec("coding", "coder-1"),
        changelog="rework")
    assert v2["agent"]["version"] == 2
    manager.test("coder-1")  # tests v2
    manager.enable("coder-1")
    with pytest.raises(LifecycleError):
        manager._transition("coder-1", AgentLifecycle.ENABLED, version=1)


def test_manager_unknown_agents_refused(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    for operation in ("enable", "pause", "resume", "disable", "retire",
                      "test", "validate"):
        with pytest.raises(AgentNotFoundError):
            getattr(manager, operation)("ghost")


def test_manager_validate_detects_corrupt_package(tmp_path):
    import json

    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    manager.create("coder-1", template="coding")
    spec_path = tmp_path / "agents" / "coder-1" / "versions" / "v1" \
        / "spec.json"
    payload = json.loads(spec_path.read_text())
    payload["permissions"] = ["read_file", "delete_repository"]
    spec_path.write_text(json.dumps(payload))
    with pytest.raises(SpecError):
        manager.validate("coder-1")


def test_manager_versions_record_lifecycle(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    manager.create("coder-1", template="coding")
    manager.test("coder-1")
    manager.enable("coder-1")
    versions = manager.versions("coder-1")
    assert versions == [{"version": 1,
                         "lifecycle": "enabled",
                         "is_current": True,
                         "created_by": "operator",
                         "changelog": "initial",
                         "operator_confirmed": False,
                         "parent_version": 0,
                         "permission_digest":
                             versions[0]["permission_digest"],
                         "created_at": versions[0]["created_at"],
                         "benchmarks": versions[0]["benchmarks"]}]


def test_manager_runtime_for_disabled_agent_is_not_runnable(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    manager.create("coder-1", template="coding")
    manager.test("coder-1")
    runtime = manager.runtime_for("coder-1")
    assert runtime.enabled is False
    with pytest.raises(NotRunnableError):
        runtime.call_model("hello")
    manager.enable("coder-1")
    runtime = manager.runtime_for("coder-1")
    assert runtime.enabled is True


def test_store_round_trips_manifest(tmp_path):
    from forge.agent_engine.spec import AgentSpec
    from forge.agent_engine.templates import build_template_spec

    store = AgentStore(tmp_path / "agents")
    spec = build_template_spec("research", "researcher-1")
    manifest = AgentManifest(
        id=spec.name, name=spec.name, version=3, lifecycle="created",
        spec=spec, permissions=spec.permissions,
        permission_digest=spec.permission_digest(),
        fingerprint=spec.fingerprint(), template="research",
        parent_version=2, created_at="2026-01-01T00:00:00+00:00",
        created_by="operator", changelog="x",
        operator_confirmed=False)
    store.save_version(manifest)
    store.set_current(spec.name, 3, "validated")
    assert store.load_manifest(spec.name) == manifest
    assert store.current(spec.name) == (3, "validated")
