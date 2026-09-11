"""Agent Creation Engine (A81): specification validation.

The specification is the foundation of every agent: name, purpose,
capabilities, tools, permissions, model requirements, memory policy,
verification requirements, and resource limits. These tests pin the
fail-closed validation contract for every field.
"""
from __future__ import annotations

import pytest

from forge.agent_engine.errors import SpecError
from forge.agent_engine.spec import (
    KNOWN_PERMISSIONS,
    NEVER_GRANTABLE,
    TOOL_PERMISSIONS,
    AgentSpec,
    MemoryPolicy,
    ModelRequirements,
    ResourceLimits,
    VerificationRequirements,
)
from forge.agent_engine.templates import TEMPLATE_IDS, build_template_spec


def _coding_payload(**overrides):
    payload = build_template_spec("coding", "helper-1").to_dict()
    payload.update(overrides)
    return payload


def test_spec_carries_all_nine_fields():
    spec = build_template_spec("coding", "helper-1")
    payload = spec.to_dict()
    for field in ("name", "purpose", "capabilities", "tools",
                  "permissions", "model", "memory", "verification",
                  "limits"):
        assert field in payload, field
    assert spec.name == "helper-1"
    assert spec.purpose
    assert spec.model.capabilities
    assert spec.verification.benchmark
    assert spec.limits.max_tokens_per_request > 0


def test_spec_validation_rejects_bad_names():
    for bad in ("", "X", "Bad Name!", "has..dots", "a",
                "a" * 60, "-leading", "trailing-"):
        with pytest.raises(SpecError):
            AgentSpec.from_dict(_coding_payload(name=bad))
    AgentSpec.from_dict(_coding_payload(name="valid-name-01"))


def test_spec_validation_requires_purpose():
    with pytest.raises(SpecError):
        AgentSpec.from_dict(_coding_payload(purpose=""))
    with pytest.raises(SpecError):
        AgentSpec.from_dict(_coding_payload(purpose="   "))
    with pytest.raises(SpecError):
        AgentSpec.from_dict(_coding_payload(purpose="x" * 2001))


def test_spec_capabilities_use_canonical_vocabulary():
    with pytest.raises(SpecError):
        AgentSpec.from_dict(_coding_payload(capabilities=["mind-reading"]))
    with pytest.raises(SpecError):
        AgentSpec.from_dict(_coding_payload(capabilities=[]))
    # Deduplication is applied before validation.
    spec = AgentSpec.from_dict(
        _coding_payload(capabilities=["coding", "coding", "testing"]))
    assert spec.capabilities == ("coding", "testing")


def test_spec_tools_come_from_the_tool_vocabulary():
    with pytest.raises(SpecError):
        AgentSpec.from_dict(_coding_payload(tools=["everything"]))
    with pytest.raises(SpecError):
        AgentSpec.from_dict(_coding_payload(tools=[]))
    # Every declared tool requires its permission to be granted.
    with pytest.raises(SpecError):
        AgentSpec.from_dict(
            _coding_payload(tools=["read_file", "write_file"],
                            permissions=["read_file"]))
    spec = AgentSpec.from_dict(
        _coding_payload(tools=["read_file", "write_file"],
                        permissions=["read_file", "write_file"]))
    assert spec.tools == ("read_file", "write_file")


def test_spec_permissions_are_least_privilege():
    # Extra permissions need an explicit rationale.
    with pytest.raises(SpecError):
        AgentSpec.from_dict(_coding_payload(permissions=["read_file"]))
    with pytest.raises(SpecError):
        AgentSpec.from_dict(
            _coding_payload(tools=["read_file"],
                            permissions=["read_file", "git_push"]))
    spec = AgentSpec.from_dict(
        _coding_payload(
            tools=["read_file"],
            permissions=["read_file", "git_push"],
            permission_rationale=[["git_push", "publishes releases"]]))
    assert "git_push" in spec.permissions


def test_spec_never_grantable_permissions_are_refused():
    for permission in NEVER_GRANTABLE:
        with pytest.raises(SpecError):
            AgentSpec.from_dict(
                _coding_payload(tools=["read_file"],
                                permissions=["read_file", permission],
                                permission_rationale=[[permission, "nope"]]))


def test_spec_model_requirements_validated():
    with pytest.raises(SpecError):
        AgentSpec.from_dict(_coding_payload(
            model={"capabilities": ["bogus"], "preference": "any",
                   "min_context_tokens": 0, "allow_fallback": True}))
    with pytest.raises(SpecError):
        AgentSpec.from_dict(_coding_payload(
            model={"capabilities": ["coding"], "preference": "cloud",
                   "min_context_tokens": 0, "allow_fallback": True}))
    with pytest.raises(SpecError):
        AgentSpec.from_dict(_coding_payload(
            model={"capabilities": ["coding"], "preference": "any",
                   "min_context_tokens": -5, "allow_fallback": True}))


def test_spec_memory_policy_validated():
    with pytest.raises(SpecError):
        AgentSpec.from_dict(_coding_payload(
            memory={"enabled": True, "max_entries": 0,
                    "max_entry_bytes": 1024, "retention": "version"}))
    with pytest.raises(SpecError):
        AgentSpec.from_dict(_coding_payload(
            memory={"enabled": True, "max_entries": 10,
                    "max_entry_bytes": 1024, "retention": "until-reboot"}))
    with pytest.raises(SpecError):
        AgentSpec.from_dict(_coding_payload(
            memory={"enabled": True, "max_entries": 10,
                    "max_entry_bytes": 0, "retention": "version"}))


def test_spec_verification_requirements_validated():
    with pytest.raises(SpecError):
        AgentSpec.from_dict(_coding_payload(
            verification={"benchmark": "", "min_score": 0.75,
                          "min_scenarios": 0, "tests_required": True,
                          "security_review": False}))
    with pytest.raises(SpecError):
        AgentSpec.from_dict(_coding_payload(
            verification={"benchmark": "coding", "min_score": 1.5,
                          "min_scenarios": 0, "tests_required": True,
                          "security_review": False}))
    with pytest.raises(SpecError):
        AgentSpec.from_dict(_coding_payload(
            verification={"benchmark": "coding", "min_score": 0.75,
                          "min_scenarios": -1, "tests_required": True,
                          "security_review": False}))


def test_spec_resource_limits_validated():
    for field in ("max_tokens_per_request", "max_requests",
                  "max_wall_seconds"):
        limits = {"max_tokens_per_request": 8192, "max_requests": 200,
                  "max_wall_seconds": 1800.0, "max_files_written": 50,
                  "working_dirs": []}
        limits[field] = 0
        with pytest.raises(SpecError):
            AgentSpec.from_dict(_coding_payload(limits=limits))
    with pytest.raises(SpecError):
        AgentSpec.from_dict(_coding_payload(
            limits={"max_tokens_per_request": 8192, "max_requests": 200,
                    "max_wall_seconds": 1800.0, "max_files_written": 50,
                    "working_dirs": ["../escape"]}))
    with pytest.raises(SpecError):
        AgentSpec.from_dict(_coding_payload(
            limits={"max_tokens_per_request": 8192, "max_requests": 200,
                    "max_wall_seconds": 1800.0, "max_files_written": 50,
                    "working_dirs": [".git"]}))
    with pytest.raises(SpecError):
        AgentSpec.from_dict(_coding_payload(
            limits={"max_tokens_per_request": 8192, "max_requests": 200,
                    "max_wall_seconds": 1800.0, "max_files_written": 50,
                    "working_dirs": ["/absolute"]}))


def test_spec_round_trip_preserves_fingerprint():
    spec = AgentSpec.from_dict(_coding_payload())
    again = AgentSpec.from_dict(spec.to_dict())
    assert again == spec
    assert again.fingerprint() == spec.fingerprint()
    assert again.permission_digest() == spec.permission_digest()


def test_permission_digest_only_covers_permissions():
    spec = AgentSpec.from_dict(_coding_payload())
    changed = AgentSpec.from_dict(
        _coding_payload(purpose="a completely different purpose"))
    assert changed.fingerprint() != spec.fingerprint()
    assert changed.permission_digest() == spec.permission_digest()


def test_all_templates_build_valid_specs():
    for template in TEMPLATE_IDS:
        spec = build_template_spec(template, f"agent-{template}")
        spec.validate()
        assert spec.tools
        assert set(spec.permissions) == set(
            TOOL_PERMISSIONS[tool] for tool in spec.tools)
        assert set(spec.permissions) <= KNOWN_PERMISSIONS


def test_unknown_template_refused():
    with pytest.raises(SpecError):
        build_template_spec("nope", "agent-1")


def test_required_template_set():
    assert TEMPLATE_IDS == ("coding", "research", "security", "game-dev",
                            "os-dev", "documentation")


def test_sub_policy_defaults():
    spec = ModelRequirements()
    assert spec.preference == "any"
    assert spec.allow_fallback is True
    memory = MemoryPolicy()
    assert memory.enabled is True
    assert memory.retention == "version"
    verification = VerificationRequirements()
    assert verification.benchmark == "generic"
    assert verification.min_score == 0.75
    limits = ResourceLimits()
    assert limits.max_files_written == 50
