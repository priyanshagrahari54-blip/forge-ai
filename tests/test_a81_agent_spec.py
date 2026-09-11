"""A81: agent specification validation."""
from __future__ import annotations

import pytest

from forge.agent_engine.spec import (
    AgentSpec,
    MemoryPolicy,
    PermissionSpec,
    ResourceLimits,
    SpecError,
    VerificationRequirements,
)

MINIMAL = {
    "name": "demo-agent",
    "purpose": "Do one narrow thing well.",
    "capabilities": ["coding"],
}


def test_minimal_spec_has_conservative_defaults():
    spec = AgentSpec.from_dict(MINIMAL)
    assert spec.name == "demo-agent"
    assert spec.capabilities == ("coding",)
    assert spec.tools == ()
    assert spec.permissions.write_paths == ()
    assert spec.permissions.allow_network is False
    assert spec.permissions.allow_terminal is False
    assert spec.permissions.require_approval_for_writes is True
    assert "security" in spec.verification.required_gates
    assert spec.memory_policy.allow_secrets is False


def test_fingerprint_is_deterministic_and_content_addressed():
    first = AgentSpec.from_dict(MINIMAL)
    second = AgentSpec.from_dict(dict(MINIMAL))
    assert first.fingerprint() == second.fingerprint()
    changed = first.with_changes(purpose="Do something else entirely.")
    assert changed.fingerprint() != first.fingerprint()


@pytest.mark.parametrize("payload,fragment", [
    ({"name": "X", "purpose": "p", "capabilities": ["coding"]}, "name"),
    ({"name": "demo-agent", "purpose": "", "capabilities": ["coding"]},
     "purpose"),
    ({"name": "demo-agent", "purpose": "p", "capabilities": []},
     "capability"),
    ({"name": "demo-agent", "purpose": "p", "capabilities": ["telepathy"]},
     "canonical"),
    ({"name": "demo-agent", "purpose": "p", "capabilities": ["coding"],
      "unexpected": 1}, "Unknown specification fields"),
])
def test_invalid_specs_are_refused(payload, fragment):
    with pytest.raises(SpecError) as excinfo:
        AgentSpec.from_dict(payload)
    assert fragment.lower() in str(excinfo.value).lower()


def test_escalation_tools_are_always_refused():
    for tool in ("grant_permission", "self_grant", "sudo", "escalate"):
        with pytest.raises(SpecError) as excinfo:
            AgentSpec.from_dict(dict(MINIMAL, tools=[tool],))
        assert "self-grant" in str(excinfo.value) or "refused" in str(
            excinfo.value)


def test_permission_scopes_cannot_escape_the_repository():
    for scope in ("../etc", "/etc/passwd", ".git/config",
                  ".forge/state.json", "~/.ssh/id_rsa"):
        with pytest.raises(SpecError):
            AgentSpec.from_dict(dict(
                MINIMAL, tools=["read_file"],
                permissions={"read_paths": [scope]}))


def test_memory_policy_never_allows_secrets():
    with pytest.raises(SpecError) as excinfo:
        MemoryPolicy.from_dict({"allow_secrets": True})
    assert "secret" in str(excinfo.value).lower()


def test_security_gate_is_always_added():
    verification = VerificationRequirements.from_dict(
        {"required_gates": ["tests"]})
    assert "security" in verification.required_gates


def test_resource_limits_are_bounded():
    with pytest.raises(SpecError):
        ResourceLimits.from_dict({"max_runs_per_hour": 100000})
    with pytest.raises(SpecError):
        ResourceLimits.from_dict({"max_concurrent_runs": 0})
    limits = ResourceLimits.from_dict({"max_runs_per_hour": 5})
    assert limits.max_runs_per_hour == 5


def test_domains_require_network_permission():
    with pytest.raises(SpecError) as excinfo:
        PermissionSpec.from_dict({"domains": ["example.com"]})
    assert "allow_network" in str(excinfo.value)


def test_primary_model_capability_must_be_declared():
    with pytest.raises(SpecError) as excinfo:
        AgentSpec.from_dict(dict(
            MINIMAL, model_requirements={"capability": "research"}))
    assert "must also be declared" in str(excinfo.value)


def test_write_scopes_need_tools():
    with pytest.raises(SpecError):
        AgentSpec.from_dict(dict(
            MINIMAL, permissions={"write_paths": ["src/**"]}))


def test_round_trip_through_dict():
    spec = AgentSpec.from_dict(dict(
        MINIMAL, tools=["read_file", "write_file"],
        permissions={"write_paths": ["src/**"]}))
    assert AgentSpec.from_dict(spec.to_dict()) == spec
