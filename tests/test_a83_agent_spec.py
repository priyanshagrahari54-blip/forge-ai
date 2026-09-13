"""A83 — agent specification validation and the six first-party templates.

The spec is the only source of an agent's power, so these tests pin the
closed vocabularies, the cross-checks between sections, and the floor
that no spec can lower.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.agents.engine import (  # noqa: E402
    FORBIDDEN_OPERATIONS,
    GRANTABLE_OPERATIONS,
    TOOL_CATALOG,
    AgentSpec,
    AgentSpecError,
    describe_templates,
    spec_from_template,
    template_ids,
    validate_spec,
)


def _payload(**overrides):
    document = {
        "name": "exporter",
        "purpose": "Adds a CSV export endpoint to the project.",
        "capabilities": ["coding"],
        "tools": [{"name": "read_file", "max_calls": 4},
                  {"name": "write_file", "max_calls": 4}],
        "permissions": {"operations": ["read_file", "write_file"],
                        "mode_ceiling": "assisted"},
    }
    document.update(overrides)
    return document


def test_spec_accepts_a_complete_specification():
    spec = validate_spec(_payload())
    assert spec.name == "exporter"
    assert spec.capabilities == ("coding",)
    assert spec.tool_names() == ("read_file", "write_file")
    assert spec.required_operations() == ("read_file", "write_file")
    assert spec.writes_files() is True
    # Every section the task requires is present and serializable.
    document = spec.to_dict()
    for key in ("name", "purpose", "capabilities", "tools", "permissions",
                "model", "memory", "verification", "limits"):
        assert key in document
    assert spec.fingerprint() == AgentSpec.from_dict(document).fingerprint()


@pytest.mark.parametrize("name", ["", "a", "Bad Name", "9lives", "x" * 60,
                                  "../escape", "ok.name!"])
def test_spec_rejects_bad_names(name):
    with pytest.raises(AgentSpecError):
        validate_spec(_payload(name=name))


def test_spec_requires_a_real_purpose():
    with pytest.raises(AgentSpecError) as info:
        validate_spec(_payload(purpose="short"))
    assert "purpose" in str(info.value)
    with pytest.raises(AgentSpecError):
        validate_spec(_payload(purpose="x" * 500))


def test_spec_rejects_unknown_capabilities():
    with pytest.raises(AgentSpecError) as info:
        validate_spec(_payload(capabilities=["mind-reading"]))
    assert "unknown capability" in str(info.value)
    with pytest.raises(AgentSpecError):
        validate_spec(_payload(capabilities=[]))


def test_spec_rejects_unknown_tools_and_duplicate_tools():
    with pytest.raises(AgentSpecError) as info:
        validate_spec(_payload(tools=[{"name": "self-destruct"}],
                               permissions={"operations": []}))
    assert "unknown tool" in str(info.value)
    with pytest.raises(AgentSpecError) as info:
        validate_spec(_payload(tools=[{"name": "read_file"},
                                      {"name": "read_file"}]))
    assert "duplicate tool" in str(info.value)


def test_spec_tool_requires_its_operation():
    """A tool without its granted operation is inconsistent, not ignored."""
    with pytest.raises(AgentSpecError) as info:
        validate_spec(_payload(permissions={"operations": ["read_file"]}))
    assert "write_file" in str(info.value)


def test_spec_rejects_blocked_operations():
    for operation in FORBIDDEN_OPERATIONS:
        with pytest.raises(AgentSpecError) as info:
            validate_spec(_payload(
                permissions={"operations": ["read_file", "write_file",
                                            operation]}))
        assert "permanently blocked" in str(info.value)


def test_spec_rejects_unknown_operations_and_modes():
    with pytest.raises(AgentSpecError):
        validate_spec(_payload(
            permissions={"operations": ["read_file", "write_file",
                                        "sudo_everything"]}))
    with pytest.raises(AgentSpecError):
        validate_spec(_payload(
            permissions={"operations": ["read_file", "write_file"],
                         "mode_ceiling": "godmode"}))


def test_spec_cannot_unprotect_runtime_state():
    for rule in (".forge", ".forge/agents", ".git", ".git/config"):
        with pytest.raises(AgentSpecError) as info:
            validate_spec(_payload(
                permissions={"operations": ["read_file", "write_file"],
                             "allowed_paths": [rule]}))
        assert "cannot un-protect" in str(info.value)


def test_spec_rejects_traversing_path_rules():
    with pytest.raises(AgentSpecError):
        validate_spec(_payload(
            permissions={"operations": ["read_file", "write_file"],
                         "denied_paths": ["../outside"]}))


def test_spec_rejects_unbounded_limits():
    with pytest.raises(AgentSpecError):
        validate_spec(_payload(limits={"max_runs_per_hour": 0}))
    with pytest.raises(AgentSpecError):
        validate_spec(_payload(limits={"max_runs_per_hour": 10_000}))
    with pytest.raises(AgentSpecError):
        validate_spec(_payload(limits={"max_wall_seconds": -1}))
    with pytest.raises(AgentSpecError):
        validate_spec(_payload(memory={"max_entries": 0}))
    with pytest.raises(AgentSpecError):
        validate_spec(_payload(memory={"scope": "everyone"}))


def test_spec_require_tests_needs_the_test_tool():
    with pytest.raises(AgentSpecError) as info:
        validate_spec(_payload(verification={"require_tests": True}))
    assert "run_tests" in str(info.value)
    ok = validate_spec(_payload(
        tools=[{"name": "read_file"}, {"name": "write_file"},
               {"name": "run_tests"}],
        permissions={"operations": ["read_file", "write_file", "run_tests"]},
        verification={"require_tests": True}))
    assert ok.verification.require_tests is True


def test_spec_reports_every_problem_at_once():
    with pytest.raises(AgentSpecError) as info:
        validate_spec({"name": "Bad", "purpose": "x", "capabilities": ["nope"],
                       "tools": [{"name": "nope"}]})
    assert len(info.value.findings) >= 4


def test_spec_defaults_are_conservative():
    spec = validate_spec(_payload())
    assert spec.model.allow_fallback is False, \
        "a run must never accept the offline placeholder by default"
    assert spec.model.prefer_local is True
    assert spec.memory.scope == "agent"
    assert spec.verification.require_security_scan is True
    assert spec.verification.min_benchmark_pass_rate == 1.0
    assert spec.permissions.mode_ceiling == "assisted"
    assert spec.limits.max_concurrent == 1


def test_fingerprint_changes_with_the_spec():
    base = validate_spec(_payload())
    same = validate_spec(_payload())
    other = validate_spec(_payload(purpose="A different, longer purpose."))
    assert base.fingerprint() == same.fingerprint()
    assert base.fingerprint() != other.fingerprint()


def test_tool_catalog_matches_grantable_operations():
    for name, info in TOOL_CATALOG.items():
        assert info.operation in GRANTABLE_OPERATIONS, name
        assert info.risk in ("NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL")
    assert not set(FORBIDDEN_OPERATIONS) & set(GRANTABLE_OPERATIONS)


# -- templates -----------------------------------------------------------


def test_all_six_templates_exist():
    assert template_ids() == ("coding", "research", "security",
                              "game-development", "os-development",
                              "documentation")
    described = {item["id"]: item for item in describe_templates()}
    assert set(described) == set(template_ids())
    assert described["coding"]["title"] == "Coding Agent"
    assert described["research"]["title"] == "Research Agent"
    assert described["security"]["title"] == "Security Agent"
    assert described["game-development"]["title"] == "Game Development Agent"
    assert described["os-development"]["title"] == "OS Development Agent"
    assert described["documentation"]["title"] == "Documentation Agent"


@pytest.mark.parametrize("template", sorted(template_ids()))
def test_every_template_produces_a_valid_spec(template):
    spec = spec_from_template(template, name="probe-agent")
    assert spec.template == template
    assert spec.findings() == ()
    assert spec.capabilities
    assert spec.model.allow_fallback is False


def test_read_only_templates_cannot_write(tmp_path):
    for template in ("research", "security"):
        spec = spec_from_template(template, name="probe-agent")
        assert spec.writes_files() is False, template
        assert spec.permissions.mode_ceiling == "safe", template
        assert "write_file" not in spec.permissions.operations


def test_writing_templates_declare_their_boundaries():
    docs = spec_from_template("documentation", name="probe-agent")
    assert docs.permissions.allowed_paths == ("docs/", "README.md")
    os_spec = spec_from_template("os-development", name="probe-agent")
    assert "run_command" in os_spec.permissions.operations
    assert os_spec.verification.require_tests is True
    assert os_spec.verification.require_review is True
    game = spec_from_template("game-development", name="probe-agent")
    assert "build/" in game.permissions.denied_paths


def test_template_overrides_are_revalidated():
    with pytest.raises(AgentSpecError):
        spec_from_template("coding", name="probe-agent",
                           overrides={"capabilities": ["telepathy"]})
    with pytest.raises(AgentSpecError):
        spec_from_template("coding", name="probe-agent",
                           overrides={"tools": [{"name": "delete_file"}]})
    tightened = spec_from_template(
        "coding", name="probe-agent",
        overrides={"limits": {"max_runs_per_hour": 5}})
    assert tightened.limits.max_runs_per_hour == 5


def test_unknown_template_is_refused():
    with pytest.raises(AgentSpecError) as info:
        spec_from_template("skynet", name="probe-agent")
    assert "Unknown template" in str(info.value)


def test_template_purpose_defaults_are_honest():
    spec = spec_from_template("coding", name="probe-agent")
    assert len(spec.purpose) >= 20
    custom = spec_from_template("coding", name="probe-agent",
                                purpose="Exports invoices to CSV nightly.")
    assert custom.purpose == "Exports invoices to CSV nightly."
