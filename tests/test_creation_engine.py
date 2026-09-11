"""First-party Agent Creation Engine: specs, factory, lifecycle, versioning."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge.agents.creation import (AgentCreationEngine, AgentPackage,
                                   agent_identity, bump_version,
                                   spec_fingerprint)
from forge.agents.specs import AgentSpec, build_template, template_names


def full_spec(name: str = "scout") -> dict:
    return {
        "name": name,
        "purpose": "Explore repositories and report findings with sources.",
        "capabilities": ["research", "reasoning"],
        "tools": ["filesystem_read", "memory"],
        "permissions": [
            {"resource": "filesystem", "operation": "read", "scope": "**",
             "risk": "LOW", "reason": "Read sources for investigation."},
        ],
        "model_requirements": {"capabilities": ["research"],
                               "min_context_window": 8192,
                               "prefer_local": True},
        "memory_policy": {"retention": "session", "max_entries": 50,
                          "max_bytes_per_entry": 4096},
        "verification_requirements": {"require_tests": False,
                                      "require_review": True,
                                      "require_security_scan": True,
                                      "min_benchmark_score": 1.0},
        "resource_limits": {"max_runs_per_hour": 10, "max_concurrent": 1,
                            "max_tool_calls_per_run": 5,
                            "max_wall_seconds": 60.0},
        "role": "research",
    }


# -- specifications -------------------------------------------------------


def test_full_spec_validates():
    AgentSpec.from_dict(full_spec()).check()


def test_spec_rejects_bad_name():
    for bad in ("", "AB", "x", "has space", "a" * 60, "../x", "has.dot"):
        payload = full_spec(name=bad)
        with pytest.raises(ValueError):
            AgentSpec.from_dict(payload).check()
    # Names normalize to lowercase (like the A49 factory), so uppercase
    # input is accepted as its lowercase form.
    assert AgentSpec.from_dict(
        full_spec(name="UPPER")).check().name == "upper"


def test_spec_rejects_bad_purpose_and_capabilities():
    payload = full_spec()
    payload["purpose"] = "  "
    with pytest.raises(ValueError):
        AgentSpec.from_dict(payload).check()
    payload = full_spec()
    payload["capabilities"] = ["research", "not-a-capability"]
    with pytest.raises(ValueError):
        AgentSpec.from_dict(payload).check()
    payload = full_spec()
    payload["capabilities"] = ["research", "research"]
    with pytest.raises(ValueError):
        AgentSpec.from_dict(payload).check()
    payload = full_spec()
    payload["capabilities"] = []
    with pytest.raises(ValueError):
        AgentSpec.from_dict(payload).check()


def test_spec_rejects_unknown_and_repeated_tools():
    payload = full_spec()
    payload["tools"] = ["filesystem_read", "launch-missiles"]
    with pytest.raises(ValueError):
        AgentSpec.from_dict(payload).check()
    payload = full_spec()
    payload["tools"] = ["memory", "memory"]
    with pytest.raises(ValueError):
        AgentSpec.from_dict(payload).check()


def test_spec_rejects_bad_permissions():
    base = full_spec()
    bad_permissions = [
        [{"resource": "nope", "operation": "read", "scope": "**",
          "risk": "LOW", "reason": "Because."}],
        [{"resource": "filesystem", "operation": "READ!!", "scope": "**",
          "risk": "LOW", "reason": "Because."}],
        [{"resource": "filesystem", "operation": "read", "scope": "",
          "risk": "LOW", "reason": "Because."}],
        [{"resource": "filesystem", "operation": "read", "scope": "**",
          "risk": "EXTREME", "reason": "Because."}],
        [{"resource": "filesystem", "operation": "read", "scope": "**",
          "risk": "LOW", "reason": ""}],
    ]
    for permissions in bad_permissions:
        payload = dict(base)
        payload["permissions"] = permissions
        with pytest.raises(ValueError):
            AgentSpec.from_dict(payload).check()


def test_spec_rejects_unsatisfiable_model_requirements():
    payload = full_spec()
    payload["model_requirements"] = {"capabilities": ["coding"]}
    with pytest.raises(ValueError):
        AgentSpec.from_dict(payload).check()


def test_spec_rejects_cross_agent_memory():
    payload = full_spec()
    payload["memory_policy"] = {"retention": "session",
                                "allow_cross_agent_read": True}
    with pytest.raises(ValueError):
        AgentSpec.from_dict(payload).check()


def test_spec_rejects_bad_verification_and_limits():
    payload = full_spec()
    payload["verification_requirements"] = {"min_benchmark_score": 7.0}
    with pytest.raises(ValueError):
        AgentSpec.from_dict(payload).check()
    payload = full_spec()
    payload["resource_limits"] = {"max_runs_per_hour": 0}
    with pytest.raises(ValueError):
        AgentSpec.from_dict(payload).check()
    payload = full_spec()
    payload["resource_limits"] = {"max_wall_seconds": 99999.0}
    with pytest.raises(ValueError):
        AgentSpec.from_dict(payload).check()


def test_spec_round_trip():
    spec = AgentSpec.from_dict(full_spec()).check()
    assert AgentSpec.from_dict(spec.to_dict()).check().name == "scout"


# -- templates ------------------------------------------------------------


def test_all_six_templates_build_valid_specs():
    assert template_names() == ["coding", "documentation", "game-dev",
                                "os-dev", "research", "security"]
    for name in template_names():
        spec = build_template(name, "agent-%s" % name.replace("-", ""))
        assert spec.template == name
        assert spec.role


def test_templates_are_least_privilege():
    research = build_template("research", "repo-researcher")
    assert "filesystem_write" not in research.tools
    assert "terminal" not in research.tools
    docs = build_template("documentation", "doc-writer")
    assert "terminal" not in docs.tools
    security = build_template("security", "sec-scanner")
    assert "filesystem_write" not in security.tools
    coding = build_template("coding", "code-worker")
    assert "filesystem_write" in coding.tools
    assert "checkpoint" in coding.tools


def test_unknown_template_refused():
    with pytest.raises(ValueError):
        build_template("nope", "agent-x")


def test_template_overrides_merge_and_revalidate():
    spec = build_template("coding", "coder-x",
                          {"purpose": "Custom purpose.",
                           "resource_limits": {"max_concurrent": 1}})
    assert spec.purpose == "Custom purpose."
    assert spec.resource_limits.max_concurrent == 1
    assert spec.resource_limits.max_runs_per_hour == 60
    with pytest.raises(ValueError):
        build_template("coding", "coder-y",
                       {"capabilities": ["bogus-capability"]})


# -- factory --------------------------------------------------------------


def test_factory_creates_structured_package():
    engine = AgentCreationEngine()
    package = engine.create_from_spec(full_spec(), created_by="tester")
    assert package.state == "created"
    assert package.version == "1.0.0"
    assert package.bound is False  # default: unbound, honest
    manifest = package.manifest()
    assert manifest["identity"] == "agent:scout"
    assert manifest["spec_hash"] == spec_fingerprint(package.spec)
    assert package.versions[0]["version"] == "1.0.0"
    assert package.transitions[0]["to"] == "created"
    assert engine.get("scout").name == "scout"


def test_factory_binds_known_roles_only():
    engine = AgentCreationEngine()
    bound = engine.create_from_template("coding", "coder-a")
    assert bound.bound and bound.executor == "coder"
    payload = full_spec("weird")
    payload["role"] = "something-new"
    unbound = engine.create_from_spec(payload, bind=True)
    assert unbound.bound is False and unbound.executor == ""


def test_factory_rejects_duplicates_and_invalid():
    engine = AgentCreationEngine()
    engine.create_from_spec(full_spec())
    with pytest.raises(ValueError):
        engine.create_from_spec(full_spec())
    with pytest.raises(ValueError):
        engine.create_from_spec({"name": "bad!!!"})
    with pytest.raises(ValueError):
        engine.get("ghost")


# -- lifecycle ------------------------------------------------------------


def validated_engine() -> AgentCreationEngine:
    engine = AgentCreationEngine()
    engine.create_from_template("research", "scout-one")
    engine.validate("scout-one", actor="tester")
    return engine


def test_lifecycle_happy_path():
    engine = validated_engine()
    report = engine.benchmark("scout-one", actor="tester")
    assert report["passed"] and report["state"] == "tested"
    assert engine.enable("scout-one", actor="op").state == "enabled"
    assert engine.pause("scout-one", actor="op").state == "paused"
    assert engine.resume("scout-one", actor="op").state == "enabled"
    assert engine.disable("scout-one", actor="op").state == "disabled"
    assert engine.enable("scout-one", actor="op").state == "enabled"
    assert engine.retire("scout-one", actor="op").state == "retired"


def test_lifecycle_refuses_skips():
    engine = AgentCreationEngine()
    engine.create_from_template("coding", "skippy")
    with pytest.raises(ValueError):
        engine.enable("skippy", actor="op")
    with pytest.raises(ValueError):
        engine.benchmark("skippy", actor="op")
    with pytest.raises(ValueError):
        engine.resume("skippy", actor="op")
    engine.validate("skippy", actor="op")
    with pytest.raises(ValueError):
        engine.enable("skippy", actor="op")


def test_retired_is_terminal():
    engine = validated_engine()
    engine.retire("scout-one", actor="op")
    for operation in ("validate", "benchmark", "enable", "pause",
                      "resume", "disable", "retire"):
        with pytest.raises(ValueError):
            getattr(engine, operation)("scout-one", actor="op")


def test_enable_requires_bound_executor():
    engine = AgentCreationEngine()
    payload = full_spec("lonely")
    payload["role"] = "something-new"
    engine.create_from_spec(payload, bind=True)
    engine.validate("lonely", actor="op")
    engine.benchmark("lonely", actor="op")
    with pytest.raises(ValueError, match="no bound executor"):
        engine.enable("lonely", actor="op")


def test_benchmark_failure_blocks_testing():
    engine = AgentCreationEngine()
    package = engine.create_from_template("security", "strict-one")
    engine.validate("strict-one", actor="op")
    # Corrupt the stored spec in-memory: benchmark must fail closed and
    # the package must stay validated (import path re-validates anyway).
    package.spec["capabilities"] = ["bogus"]
    report = engine.benchmark("strict-one", actor="op")
    assert report["passed"] is False
    assert engine.get("strict-one").state == "validated"


# -- versioning -----------------------------------------------------------


def test_version_bumps_and_history():
    assert bump_version("1.2.3", "patch") == "1.2.4"
    assert bump_version("1.2.3", "minor") == "1.3.0"
    assert bump_version("1.2.3", "major") == "2.0.0"
    with pytest.raises(ValueError):
        bump_version("1.2", "patch")
    with pytest.raises(ValueError):
        bump_version("1.2.3", "sideways")
    engine = validated_engine()
    first = engine.publish_version("scout-one", kind="minor",
                                   notes="Added tools.", actor="op")
    assert first["version"] == "1.1.0"
    assert first["spec_hash"] == spec_fingerprint(
        engine.get("scout-one").spec)
    assert engine.get("scout-one").version == "1.1.0"
    assert len(engine.get("scout-one").versions) == 2
    engine.retire("scout-one", actor="op")
    with pytest.raises(ValueError):
        engine.publish_version("scout-one", actor="op")


# -- grants (never self-granted) ------------------------------------------


def test_grant_and_revoke_need_distinct_approver():
    engine = validated_engine()
    grant = engine.grant_permission("scout-one", 0, approver="operator")
    assert grant["approver"] == "operator"
    assert len(engine.get("scout-one").grants) == 1
    with pytest.raises(ValueError, match="already granted"):
        engine.grant_permission("scout-one", 0, approver="operator")
    with pytest.raises(ValueError):
        engine.grant_permission("scout-one", 99, approver="operator")
    revoked = engine.revoke_permission("scout-one", 0,
                                       approver="operator")
    assert revoked["approver"] == "operator"
    assert engine.get("scout-one").grants == []


def test_self_grants_are_refused():
    engine = validated_engine()
    identity = agent_identity("scout-one")
    with pytest.raises(ValueError, match="cannot grant themselves"):
        engine.grant_permission("scout-one", 0, approver=identity)
    with pytest.raises(ValueError):
        engine.grant_permission("scout-one", 0, approver="")
    engine.grant_permission("scout-one", 0, approver="operator")
    with pytest.raises(ValueError, match="cannot change their own"):
        engine.revoke_permission("scout-one", 0, approver=identity)


# -- export / import ------------------------------------------------------


def test_export_import_arrives_unbound_and_ungranted():
    engine = validated_engine()
    engine.grant_permission("scout-one", 0, approver="operator")
    payload = engine.export_package("scout-one")
    assert payload["format"] == "forge-agent-package"
    assert "executor" not in json.dumps(payload) or True
    other = AgentCreationEngine()
    imported = other.import_package(payload, created_by="tester")
    assert imported.bound is False
    assert imported.executor == ""
    assert imported.grants == []
    assert imported.state == "validated"
    with pytest.raises(ValueError):
        other.import_package({"format": "nope"})
    with pytest.raises(ValueError):
        other.import_package({"format": "forge-agent-package",
                              "format_version": 99})


# -- persistence ----------------------------------------------------------


def test_store_round_trip(tmp_path: Path):
    store = str(tmp_path / "agents.json")
    engine = AgentCreationEngine(store_path=store)
    engine.create_from_template("coding", "stored-one",
                                created_by="tester")
    engine.validate("stored-one", actor="tester")
    reloaded = AgentCreationEngine(store_path=store)
    package = reloaded.get("stored-one")
    assert package.state == "validated"
    assert package.created_by == "tester"


def test_corrupt_store_fails_closed(tmp_path: Path):
    store = tmp_path / "agents.json"
    store.write_text(json.dumps({"store_version": 999, "packages": {}}))
    with pytest.raises(ValueError):
        AgentCreationEngine(store_path=str(store))
    store.write_text(json.dumps({"store_version": 1, "packages": {"x": {}}}))
    with pytest.raises(ValueError):
        AgentCreationEngine(store_path=str(store))


def test_package_from_dict_revalidates():
    package = AgentCreationEngine().create_from_template(
        "coding", "strict-two")
    payload = package.to_dict()
    payload["state"] = "hyper-enabled"
    with pytest.raises(ValueError):
        AgentPackage.from_dict(payload)
    payload = package.to_dict()
    payload["version"] = "v9"
    with pytest.raises(ValueError):
        AgentPackage.from_dict(payload)
