"""A81: the agent factory builds structured, envelope-faithful packages."""
from __future__ import annotations

import json

import pytest

from forge.agent_engine.factory import AgentFactoryEngine
from forge.agent_engine.lifecycle import CREATED
from forge.agent_engine.package import AgentPackage
from forge.agent_engine.spec import AgentSpec, SpecError
from forge.agent_engine.validator import PackageValidator

SPEC = {
    "name": "builder-agent",
    "purpose": "Build things carefully.",
    "capabilities": ["coding", "testing"],
    "tools": ["read_file", "write_file", "run_tests"],
    "permissions": {"read_paths": ["src/**"], "write_paths": ["src/**"]},
    "model_requirements": {"capability": "coding"},
    "memory_policy": {"scope": "session"},
    "verification": {"required_gates": ["tests", "security"]},
}


@pytest.fixture()
def package():
    return AgentFactoryEngine().build(SPEC, built_by="operator")


def test_package_is_structured_and_content_addressed(package):
    manifest = package.manifest()
    assert manifest["format"] == "forge-agent-package"
    assert manifest["name"] == "builder-agent"
    assert manifest["version"] == "1.0.0"
    assert manifest["spec_fingerprint"] == package.spec.fingerprint()
    assert package.state == CREATED
    rebuilt = AgentFactoryEngine().build(SPEC)
    assert rebuilt.package_id() == package.package_id()
    assert rebuilt.prompt == package.prompt


def test_package_wires_every_required_subsystem(package):
    runtime = package.runtime
    assert set(runtime) >= {"model_fabric", "policy_gate", "tool_runtime",
                            "memory", "verification", "checkpoints",
                            "resource_limits"}
    assert runtime["model_fabric"]["capability"] == "coding"
    assert runtime["memory"]["namespace"] == "agents/builder-agent"
    assert "security" in runtime["verification"]["gates"]
    assert runtime["checkpoints"]["enabled"] is True
    assert runtime["policy_gate"]["self_grant"] is False


def test_tool_grants_mirror_the_declared_envelope(package):
    grants = {g["tool"]: g for g in package.runtime["tool_runtime"]["grants"]}
    assert grants["write_file"]["writes"] is True
    assert grants["write_file"]["requires_approval"] is True
    assert grants["write_file"]["scopes"] == ["src/**"]
    assert grants["read_file"]["writes"] is False
    assert grants["read_file"]["scopes"] == ["src/**"]


def test_factory_refuses_tools_beyond_the_envelope():
    factory = AgentFactoryEngine()
    with pytest.raises(SpecError):
        factory.build(dict(SPEC, tools=["read_file", "terminal"],
                           permissions={"read_paths": ["**"]}))
    with pytest.raises(SpecError):
        factory.build(dict(SPEC, tools=["read_file", "web_fetch"],
                           permissions={"read_paths": ["**"]}))
    with pytest.raises(SpecError):
        factory.build(dict(SPEC, tools=["read_file", "git_commit"],
                           permissions={"read_paths": ["**"]}))


def test_prompt_states_the_no_self_grant_rule(package):
    assert "cannot grant yourself" in package.prompt
    assert "builder-agent" in package.prompt


def test_built_package_validates(package):
    result = PackageValidator().validate(package)
    assert result.valid
    assert not result.blocking


def test_export_import_round_trip_resets_trust(package):
    package.state = "enabled"
    payload = json.loads(package.to_json())
    restored = AgentPackage.from_dict(payload)
    assert restored.spec == package.spec
    assert restored.state == CREATED
    assert restored.benchmark == {}


def test_tampered_export_is_refused(package):
    payload = package.to_dict()
    payload["spec"]["purpose"] = "Something completely different."
    with pytest.raises(SpecError):
        AgentPackage.from_dict(payload)


def test_unknown_tools_are_reported_not_silently_granted():
    spec = AgentSpec.from_dict(dict(SPEC, tools=["read_file", "telepathy"]))
    package = AgentFactoryEngine().build(spec)
    assert package.runtime["tool_runtime"]["unknown_tools"] == ["telepathy"]
    findings = PackageValidator().validate(package).findings
    assert any(f.code == "unknown_tool" for f in findings)
