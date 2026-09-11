"""Agent Creation Engine (A81): the factory and the no-self-grant rule.

The factory generates structured agent packages from specifications and
enforces versioned, monotone permission sets. An agent may never
self-grant permissions: escalations require explicit operator
confirmation, are bounded by spec validation, and are permanently
audited.
"""
from __future__ import annotations

import json

import pytest

from forge.agent_engine.errors import (
    AgentNotFoundError,
    PermissionEscalationError,
    SpecError,
)
from forge.agent_engine.factory import AgentFactory
from forge.agent_engine.manager import AgentManager
from forge.agent_engine.store import AgentStore
from forge.agent_engine.templates import (
    TEMPLATE_IDS,
    build_template_spec,
    template_catalog,
)


def test_factory_generates_structured_package(tmp_path):
    factory = AgentFactory(AgentStore(tmp_path / "agents"))
    manifest = factory.create(build_template_spec("coding", "coder-1"))
    assert manifest.version == 1
    assert manifest.lifecycle == "created"
    package = factory.render_package("coder-1")
    assert set(package) == {"agent.json", "spec.json", "permissions.lock"}
    agent_json = json.loads(package["agent.json"])
    assert agent_json["name"] == "coder-1"
    assert agent_json["version"] == 1
    spec_json = json.loads(package["spec.json"])
    assert spec_json["tools"] == list(build_template_spec(
        "coding", "coder-1").tools)
    lock = json.loads(package["permissions.lock"])
    assert lock["permissions"] == list(manifest.permissions)
    assert lock["digest"] == manifest.spec.permission_digest()
    # The package layout is also real on disk.
    for filename in package:
        assert (tmp_path / "agents" / "coder-1" / "versions" / "v1"
                / filename).exists()


def test_factory_refuses_duplicate_agents(tmp_path):
    factory = AgentFactory(AgentStore(tmp_path / "agents"))
    factory.create(build_template_spec("coding", "coder-1"))
    with pytest.raises(SpecError):
        factory.create(build_template_spec("coding", "coder-1"))


def test_factory_refuses_identity_change_on_version(tmp_path):
    factory = AgentFactory(AgentStore(tmp_path / "agents"))
    factory.create(build_template_spec("coding", "coder-1"))
    with pytest.raises(SpecError):
        factory.create_version("coder-1",
                               build_template_spec("coding", "renamed"))
    with pytest.raises(AgentNotFoundError):
        factory.create_version("ghost", build_template_spec("coding",
                                                            "ghost"))


def test_no_self_grant_escalation_refused_without_confirmation(tmp_path):
    factory = AgentFactory(AgentStore(tmp_path / "agents"))
    factory.create(build_template_spec("coding", "coder-1"))
    escalated = build_template_spec("coding", "coder-1").to_dict()
    escalated["tools"] += ["git_push"]
    escalated["permissions"] += ["git_push"]
    with pytest.raises(PermissionEscalationError):
        factory.create_version("coder-1", escalated, changelog="push!")
    # Nothing was written: still one version, unchanged permissions.
    store = factory.store
    assert store.versions("coder-1") == [1]
    assert store.load_manifest("coder-1").permissions == tuple(
        build_template_spec("coding", "coder-1").permissions)


def test_escalation_with_operator_confirmation_is_audited(tmp_path):
    factory = AgentFactory(AgentStore(tmp_path / "agents"))
    factory.create(build_template_spec("coding", "coder-1"))
    escalated = build_template_spec("coding", "coder-1").to_dict()
    escalated["tools"] += ["git_push"]
    escalated["permissions"] += ["git_push"]
    v2 = factory.create_version("coder-1", escalated,
                                changelog="add push",
                                operator_confirmed=True)
    assert v2.version == 2
    assert v2.operator_confirmed is True
    assert "git_push" in v2.permissions
    events = factory.store.history("coder-1")
    escalation_events = [event for event in events
                         if event["event"] == "versioned"]
    assert escalation_events[-1]["escalated_permissions"] == ["git_push"]
    assert escalation_events[-1]["operator_confirmed"] is True


def test_permission_shrink_needs_no_confirmation(tmp_path):
    factory = AgentFactory(AgentStore(tmp_path / "agents"))
    factory.create(build_template_spec("coding", "coder-1"))
    shrunk = build_template_spec("research", "coder-1")
    v2 = factory.create_version("coder-1", shrunk, changelog="read-only")
    assert v2.version == 2
    assert set(v2.permissions) <= {"read_file", "search_files",
                                   "git_status"}
    assert v2.operator_confirmed is False


def test_even_confirmed_escalation_is_bounded_by_validation(tmp_path):
    factory = AgentFactory(AgentStore(tmp_path / "agents"))
    factory.create(build_template_spec("coding", "coder-1"))
    escalated = build_template_spec("coding", "coder-1").to_dict()
    escalated["permissions"] += ["delete_repository"]
    with pytest.raises(SpecError):  # never grantable, even confirmed
        factory.create_version("coder-1", escalated,
                               operator_confirmed=True)
    with pytest.raises(SpecError):  # unknown permission
        escalated["permissions"] = ["read_file", "nonsense"]
        factory.create_version("coder-1", escalated,
                               operator_confirmed=True)


def test_all_six_templates_create_and_pass_benchmarks(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    for template in TEMPLATE_IDS:
        name = f"agent-{template}"
        created = manager.create(name, template=template)
        assert created["agent"]["template"] == template
        tested = manager.test(name)
        assert tested["current_lifecycle"] == "tested", template
        assert tested["benchmark"]["score"] >= tested["agent"]["spec"][
            "verification"]["min_score"]


def test_template_catalog_describes_every_template():
    catalog = {item["id"]: item for item in template_catalog()}
    assert set(catalog) == set(TEMPLATE_IDS)
    for template in TEMPLATE_IDS:
        assert catalog[template]["tools"]
        assert catalog[template]["benchmark"]
        assert catalog[template]["description"]


def test_factory_export_package(tmp_path):
    factory = AgentFactory(AgentStore(tmp_path / "agents"))
    factory.create(build_template_spec("security", "guard-1"))
    exported = factory.export_package("guard-1")
    assert exported["agent"]["name"] == "guard-1"
    assert set(exported["files"]) == {"agent.json", "spec.json",
                                      "permissions.lock"}
