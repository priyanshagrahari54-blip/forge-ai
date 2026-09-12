"""A81 — agent factory, structured packages, lifecycle, and versioning.

These tests pin the promotion rules (nothing is enabled on a claim), the
on-disk package layout, and the versioning behaviour that discards
evidence whenever a specification changes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a81 import make_engine, make_project  # noqa: E402

from forge.agents.engine import (  # noqa: E402
    AgentExistsError,
    AgentLifecycleError,
    AgentNotFoundError,
    AgentPermissionError,
    AgentState,
    AgentVersionError,
    PackageStore,
)


@pytest.fixture()
def engine(tmp_path):
    make_project(tmp_path / "demo")
    return make_engine(tmp_path / "demo")


def _package_dir(engine, name):
    return Path(engine.root) / ".forge" / "agents" / name


# -- creation & package structure ----------------------------------------


def test_create_writes_a_structured_package(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    directory = _package_dir(engine, "exporter")
    assert (directory / "agent.json").is_file()
    assert (directory / "grants.json").is_file()
    assert (directory / "versions" / "1.0.0.json").is_file()
    manifest = json.loads((directory / "agent.json").read_text("utf-8"))
    assert manifest["format"] == "forge-agent-package"
    assert manifest["format_version"] == 1
    assert manifest["name"] == "exporter"
    assert manifest["created_by"] == "alice"
    assert manifest["lifecycle"]["state"] == AgentState.CREATED
    assert manifest["fingerprint"]
    for section in ("permissions", "model", "memory", "verification",
                    "limits", "tools", "capabilities"):
        assert section in manifest["spec"]


def test_creation_requires_a_named_operator(engine):
    from forge.agents.engine import spec_from_template

    spec = spec_from_template("coding", name="exporter")
    with pytest.raises(AgentPermissionError):
        engine.factory.create(spec, actor="  ")


def test_create_refuses_duplicates(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    from forge.agents.engine import AgentExistsError as Exists

    with pytest.raises(Exists):
        engine.create_from_template("coding", "exporter", actor="alice")


def test_store_confines_agent_names(engine):
    store = PackageStore(engine.root)
    for name in ("../escape", ".git", "Bad Name", "a", ""):
        with pytest.raises(Exception):
            store.directory_for(name)
    assert store.exists("../escape") is False


def test_creation_grants_nothing_by_default(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    permissions = engine.permissions("exporter")
    assert permissions["granted"] == []
    assert permissions["not_granted"] == sorted(permissions["ceiling"])


def test_create_with_grant_still_stays_inside_the_spec(engine):
    engine.create_from_template("coding", "exporter", actor="alice",
                                grant=True)
    permissions = engine.permissions("exporter")
    assert set(permissions["granted"]) == set(permissions["ceiling"])


# -- validation ----------------------------------------------------------


def test_validate_promotes_to_validated(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    report = engine.validate("exporter", actor="alice")
    assert report["passed"] is True
    assert {check["name"] for check in report["checks"]} >= {
        "spec", "tools", "permissions", "model", "verification", "package"}
    assert engine.get("exporter").state == AgentState.VALIDATED


def test_validate_reports_failure_without_promoting(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    package = engine.get("exporter")
    # Corrupt the stored spec: a tool that no longer exists.
    manifest_path = package.directory / "agent.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["spec"]["tools"].append({"name": "teleport", "max_calls": 1})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = engine.validate("exporter", actor="alice")
    assert report["passed"] is False
    assert any("teleport" in finding for finding in report["findings"])
    assert engine.get("exporter").state == AgentState.CREATED


def test_validate_detects_a_spec_with_no_version_record(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    record = _package_dir(engine, "exporter") / "versions" / "1.0.0.json"
    record.unlink()
    report = engine.validate("exporter", actor="alice")
    assert report["passed"] is False
    assert any(check["name"] == "package" and not check["passed"]
               for check in report["checks"])


# -- lifecycle -----------------------------------------------------------


def test_full_lifecycle_walk(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    assert engine.get("exporter").state == AgentState.CREATED
    engine.validate("exporter", actor="alice")
    assert engine.get("exporter").state == AgentState.VALIDATED
    with pytest.raises(AgentLifecycleError):
        engine.enable("exporter", actor="alice")
    engine.test("exporter", actor="alice")
    assert engine.get("exporter").state == AgentState.TESTED
    engine.enable("exporter", actor="alice")
    assert engine.get("exporter").state == AgentState.ENABLED
    engine.pause("exporter", actor="alice")
    assert engine.get("exporter").state == AgentState.PAUSED
    engine.resume("exporter", actor="alice")
    assert engine.get("exporter").state == AgentState.ENABLED
    engine.disable("exporter", actor="alice")
    assert engine.get("exporter").state == AgentState.DISABLED
    engine.retire("exporter", actor="alice")
    assert engine.get("exporter").state == AgentState.RETIRED


def test_illegal_transitions_are_refused(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    with pytest.raises(AgentLifecycleError):
        engine.pause("exporter", actor="alice")
    with pytest.raises(AgentLifecycleError):
        engine.enable("exporter", actor="alice")
    with pytest.raises(AgentLifecycleError):
        engine.retire("exporter", actor="alice")
    with pytest.raises(AgentLifecycleError):
        engine.enable("exporter", actor="alice")
    assert engine.get("exporter").state == AgentState.CREATED


def test_retired_is_terminal(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    engine.validate("exporter", actor="alice")
    engine.disable("exporter", actor="alice")
    engine.retire("exporter", actor="alice")
    with pytest.raises(AgentLifecycleError):
        engine.enable("exporter", actor="alice")
    with pytest.raises(AgentLifecycleError):
        engine.validate("exporter", actor="alice")
    with pytest.raises(AgentLifecycleError):
        engine.test("exporter", actor="alice")
    with pytest.raises(AgentLifecycleError):
        engine.factory.update_spec(
            "exporter", engine.get("exporter").spec, actor="alice")


def test_transitions_record_actor_and_reason(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    engine.validate("exporter", actor="bob")
    engine.disable("exporter", actor="carol", reason="incident 42")
    history = engine.get("exporter").lifecycle.history
    assert [(item.from_state, item.to_state, item.actor)
            for item in history] == [
        ("created", "validated", "bob"),
        ("validated", "disabled", "carol")]
    assert history[-1].reason == "incident 42"


def test_test_requires_a_validated_agent(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    with pytest.raises(AgentLifecycleError):
        engine.test("exporter", actor="alice")


def test_enable_requires_a_passing_benchmark(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    engine.validate("exporter", actor="alice")
    package = engine.get("exporter")
    package.lifecycle.benchmark = {"passed": True}
    # Force the state machine into TESTED without a real report on file,
    # then clear the evidence: enabling must fail.
    package.lifecycle.state = AgentState.TESTED
    package.lifecycle.benchmark = {}
    engine.store.write_manifest(package)
    with pytest.raises(AgentLifecycleError):
        engine.enable("exporter", actor="alice")


def test_failing_benchmark_does_not_promote(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    engine.validate("exporter", actor="alice")
    # Tamper with the stored spec (still valid, but no longer matching the
    # recorded version fingerprint), so spec-integrity must fail.
    manifest_path = _package_dir(engine, "exporter") / "agent.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["spec"]["purpose"] = "A silently edited purpose statement."
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = engine.test("exporter", actor="alice")
    assert report["passed"] is False
    assert any(scenario["name"] == "spec-integrity"
               and scenario["status"] == "failed"
               for scenario in report["scenarios"])
    assert engine.get("exporter").state == AgentState.VALIDATED
    assert engine.benchmarks("exporter"), "the failure is still recorded"


def test_unreadable_package_is_refused_not_repaired(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    manifest_path = _package_dir(engine, "exporter") / "agent.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["spec"]["limits"]["max_runs_per_hour"] = 99999
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    from forge.agents.engine import AgentPackageError

    with pytest.raises(AgentPackageError):
        engine.get("exporter")


# -- versioning ----------------------------------------------------------


def test_patch_minor_and_major_bumps(engine):
    from forge.agents.engine import spec_from_template

    engine.create_from_template("coding", "exporter", actor="alice")

    patched = spec_from_template(
        "coding", name="exporter",
        overrides={"tags": ["nightly"]})
    result = engine.update_spec("exporter", patched, actor="alice")
    assert result["change"] == "patch"
    assert result["version"] == "1.0.1"

    minor = spec_from_template(
        "coding", name="exporter", overrides={"tags": ["nightly"],
                                              "limits": {
                                                  "max_runs_per_hour": 7}})
    result = engine.update_spec("exporter", minor, actor="alice")
    assert result["change"] == "minor"
    assert result["version"] == "1.1.0"

    major = spec_from_template(
        "coding", name="exporter",
        overrides={"tags": ["nightly"], "limits": {"max_runs_per_hour": 7},
                   "tools": [{"name": "read_file"}, {"name": "write_file"},
                             {"name": "run_tests"}, {"name": "delete_file"}],
                   "permissions": {
                       "operations": ["read_file", "write_file", "run_tests",
                                      "delete_file"],
                       "mode_ceiling": "assisted"}})
    result = engine.update_spec("exporter", major, actor="alice")
    assert result["change"] == "major"
    assert result["version"] == "2.0.0"


def test_spec_change_discards_evidence_and_grants(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    engine.validate("exporter", actor="alice")
    engine.grant_spec("exporter", actor="alice")
    engine.test("exporter", actor="alice")
    engine.enable("exporter", actor="alice")

    from forge.agents.engine import spec_from_template

    widened = spec_from_template(
        "coding", name="exporter",
        overrides={"tools": [{"name": "read_file"}, {"name": "write_file"},
                             {"name": "run_tests"}, {"name": "terminal"}],
                   "permissions": {
                       "operations": ["read_file", "write_file", "run_tests",
                                      "run_command"],
                       "mode_ceiling": "assisted"}})
    result = engine.update_spec("exporter", widened, actor="alice")
    assert result["change"] == "major"
    assert engine.get("exporter").state == AgentState.CREATED
    assert engine.get("exporter").lifecycle.benchmark == {}
    assert engine.get("exporter").lifecycle.validation == {}
    # Every previously granted operation is revoked by a major change,
    # including write access; the newly widened ceiling grants nothing.
    assert set(result["revoked_operations"]) == {
        "read_file", "search_files", "git_status", "run_tests", "write_file"}
    assert engine.permissions("exporter")["granted"] == []
    with pytest.raises(AgentLifecycleError):
        engine.enable("exporter", actor="alice")


def test_version_records_are_immutable(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    with pytest.raises(AgentVersionError):
        engine.store.record_version(engine.get("exporter"), notes="again")
    versions = engine.versions("exporter")
    assert [item["version"] for item in versions] == ["1.0.0"]
    assert versions[0]["recorded_by"] == "alice"
    with pytest.raises(AgentVersionError):
        engine.version("exporter", "9.9.9")


def test_revert_creates_a_new_version(engine):
    from forge.agents.engine import spec_from_template

    engine.create_from_template("coding", "exporter", actor="alice")
    engine.update_spec(
        "exporter",
        spec_from_template("coding", name="exporter",
                           overrides={"tags": ["v2"]}),
        actor="alice")
    assert engine.get("exporter").version == "1.0.1"
    result = engine.revert_to_version("exporter", "1.0.0", actor="alice")
    assert result["version"] == "1.0.2"
    assert engine.get("exporter").spec.tags == ()


def test_unchanged_spec_is_a_no_op(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    result = engine.update_spec("exporter", engine.get("exporter").spec,
                                actor="alice")
    assert result["change"] == "none"
    assert engine.get("exporter").version == "1.0.0"


# -- reads & cleanup -----------------------------------------------------


def test_list_and_detail(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    engine.create_from_template("research", "scout", actor="alice")
    names = [item["name"] for item in engine.list()]
    assert names == ["exporter", "scout"]
    assert [item["name"] for item in engine.list(state="created")] == names
    detail = engine.detail("exporter")
    assert detail["summary"]["state"] == "created"
    assert detail["permissions"]["granted"] == []
    assert detail["memory"]["scope"] == "agent"
    assert detail["versions"][0]["version"] == "1.0.0"
    assert detail["allowed_transitions"]


def test_unknown_agent_raises(engine):
    with pytest.raises(AgentNotFoundError):
        engine.get("ghost")
    with pytest.raises(AgentNotFoundError):
        engine.delete("ghost", actor="alice")


def test_delete_requires_retirement(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    with pytest.raises(AgentLifecycleError):
        engine.delete("exporter", actor="alice")
    engine.disable("exporter", actor="alice")
    engine.retire("exporter", actor="alice")
    engine.delete("exporter", actor="alice")
    assert engine.names() == []
    assert AgentExistsError is not None


def test_corrupt_manifest_is_reported_not_guessed(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    manifest = _package_dir(engine, "exporter") / "agent.json"
    manifest.write_text("{not json", encoding="utf-8")
    from forge.agents.engine import AgentPackageError

    with pytest.raises(AgentPackageError):
        engine.get("exporter")


def test_state_transitions_are_listed_for_the_ui(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    assert set(engine.detail("exporter")["allowed_transitions"]) == {
        AgentState.VALIDATED, AgentState.DISABLED, AgentState.CREATED}
