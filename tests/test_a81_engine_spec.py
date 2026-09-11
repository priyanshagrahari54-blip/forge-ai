"""Agent Creation Engine (A81): specification validation.

A specification is the operator-authored contract. These tests pin the
validation rules: canonical vocabularies, tool-permission coupling,
bounds, round-trip serialization, and immutability (an agent can never
edit its own contract at runtime).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.agents.engine.spec import (  # noqa: E402
    AgentSpecification,
    MemoryPolicy,
    ModelRequirements,
    ResourceLimits,
    VerificationRequirements,
    validate_specification,
)
from forge.agents.engine.templates import (  # noqa: E402
    TEMPLATE_NAMES,
    spec_from_template,
    template_names,
    template_summaries,
)


def base_spec(**overrides) -> AgentSpecification:
    data = {
        "name": "csv-exporter",
        "purpose": "Export repository data as CSV",
        "capabilities": ["coding"],
        "tools": ["read_file", "write_file"],
        "permissions": ["filesystem:read", "filesystem:write"],
    }
    data.update(overrides)
    return AgentSpecification.from_dict(data)


def test_spec_roundtrip_all_nine_fields():
    spec = AgentSpecification.from_dict({
        "name": "full-agent",
        "purpose": "Does everything a spec can describe",
        "capabilities": ["coding", "testing"],
        "tools": ["read_file", "write_file", "run_tests"],
        "permissions": ["filesystem:read", "filesystem:write",
                        "terminal:execute"],
        "model_requirements": {"capabilities": ["coding"],
                               "min_context_tokens": 8192,
                               "routing_policy": "balanced"},
        "memory_policy": {"enabled": True, "scope": "agent",
                          "max_entries": 50},
        "verification": {"run_tests": True, "run_review": True,
                         "run_security": True, "require_all": True},
        "resource_limits": {"max_runs_per_hour": 10,
                            "max_concurrent": 1,
                            "max_tool_calls_per_run": 15,
                            "max_output_tokens": 2048,
                            "timeout_seconds": 300.0},
    })
    spec.validate()
    assert validate_specification(spec) == []
    restored = AgentSpecification.from_dict(spec.to_dict())
    assert restored == spec
    assert spec.primary_capability == "coding"


def test_spec_name_purpose_and_vocabulary_validation():
    with pytest.raises(ValueError):
        base_spec(name="Bad Name!")
    with pytest.raises(ValueError):
        base_spec(name="x")  # too short
    with pytest.raises(ValueError):
        base_spec(purpose="   ")
    with pytest.raises(ValueError):
        base_spec(capabilities=["mind-reading"])  # not in vocabulary
    with pytest.raises(ValueError):
        base_spec(capabilities=[])
    with pytest.raises(ValueError):
        base_spec(tools=["format_disk"])  # not a known tool
    with pytest.raises(ValueError):
        base_spec(permissions=["filesystem:format"])  # unknown operation
    with pytest.raises(ValueError):
        base_spec(permissions=["notaresource:read"])
    assert base_spec(name="good-name").name == "good-name"


def test_spec_tools_must_be_covered_by_permissions():
    # A tool without its mapped permission is unexecutable and refused.
    with pytest.raises(ValueError, match="filesystem:write"):
        base_spec(tools=["read_file", "write_file"],
                  permissions=["filesystem:read"])
    spec = base_spec(tools=["read_file"], permissions=["filesystem:read"])
    assert spec.tools == ("read_file",)


def test_spec_substructure_validation():
    with pytest.raises(ValueError):
        base_spec(model_requirements={"routing_policy": "yolo"})
    with pytest.raises(ValueError):
        base_spec(model_requirements={"capabilities": ["telepathy"]})
    with pytest.raises(ValueError):
        base_spec(memory_policy={"scope": "global"})
    with pytest.raises(ValueError):
        base_spec(memory_policy={"max_entries": 0})
    with pytest.raises(ValueError):
        base_spec(resource_limits={"max_concurrent": 99})
    with pytest.raises(ValueError):
        base_spec(resource_limits={"timeout_seconds": 0.1})
    # sane values pass
    spec = base_spec(
        model_requirements=ModelRequirements.from_dict(
            {"routing_policy": "privacy"}),
        memory_policy=MemoryPolicy.from_dict({"max_entries": 10}),
        verification=VerificationRequirements.from_dict(
            {"run_tests": False}),
        resource_limits=ResourceLimits.from_dict(
            {"max_runs_per_hour": 5}),
    )
    spec.validate()


def test_spec_permissions_dedupe_and_ceiling_query():
    spec = base_spec(
        capabilities=["coding", "coding"],
        permissions=["filesystem:read", "filesystem:read",
                     "filesystem:write"])
    assert spec.permissions == ("filesystem:read", "filesystem:write")
    assert spec.allows("filesystem", "read")
    assert spec.allows("filesystem", "write")
    assert not spec.allows("filesystem", "delete")
    assert not spec.allows("terminal", "execute")
    assert not spec.allows("made-up-resource", "read")


def test_spec_is_frozen():
    """No agent can edit its own contract — specs are immutable."""
    spec = base_spec()
    with pytest.raises(Exception):
        spec.permissions = ("filesystem:read", "filesystem:write",
                            "terminal:execute")  # type: ignore[misc]
    with pytest.raises(Exception):
        spec.name = "renamed"  # type: ignore[misc]


def test_templates_six_kinds_and_valid_specs():
    assert template_names() == list(TEMPLATE_NAMES)
    assert TEMPLATE_NAMES == ("coding", "research", "security",
                              "game-dev", "os-dev", "documentation")
    summaries = template_summaries()
    assert len(summaries) == 6
    for name in TEMPLATE_NAMES:
        spec = spec_from_template(name, name=f"{name}-agent")
        spec.validate()
        assert spec.template == name
        assert spec.purpose
        # Every template's tools are covered by its permission ceiling.
        assert all(spec.tools)
    with pytest.raises(ValueError):
        spec_from_template("not-a-template", name="x-agent")


def test_template_overrides_are_revalidated():
    # The research template grants no write permission, so a write tool
    # override must be refused by the tool-permission coupling.
    with pytest.raises(ValueError, match="filesystem:write"):
        spec_from_template(
            "research", name="reader-2",
            overrides={"tools": ["write_file"]})
    # A consistent override passes and is honored.
    spec = spec_from_template(
        "research", name="reader-1",
        overrides={"tools": ["read_file", "search", "git_status"],
                   "permissions": ["filesystem:read", "git:status"]})
    assert spec.tools == ("read_file", "search", "git_status")
    # Unknown override keys are refused.
    with pytest.raises(ValueError, match="Unknown template override"):
        spec_from_template("coding", name="reader-3",
                           overrides={"nope": 1})


def test_template_purposes_and_shapes():
    coding = spec_from_template("coding", name="coder-1")
    research = spec_from_template("research", name="reader-1")
    security = spec_from_template("security", name="sec-1")
    game = spec_from_template("game-dev", name="game-1")
    os_dev = spec_from_template("os-dev", name="osdev-1")
    docs = spec_from_template("documentation", name="docs-1")
    assert "write_file" in coding.tools
    assert "write_file" not in research.tools  # read-only by design
    assert "write_file" not in security.tools  # audit-only by design
    assert "write_file" in game.tools and "write_file" in os_dev.tools
    assert "write_file" in docs.tools
    # OS development is the most conservative on run budgets.
    assert os_dev.resource_limits.max_runs_per_hour <= \
        coding.resource_limits.max_runs_per_hour
    # Security agents never request write scopes.
    assert "filesystem:write" not in security.permissions
    assert research.memory_policy.enabled is True
