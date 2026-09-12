"""A81 agent role specifications.

The ten canonical roles must each carry a complete, machine-checkable
specification: capability, permissions, model requirement, task scope,
resource limits, timeout, and result schema — and the result schema
must actually validate structured results (conforming accepted,
violations rejected, never silently passed).
"""
from __future__ import annotations

import pytest

from forge.orchestration.roles import (AGENT_ROLES, CODER, PLANNER,
                                       RoleResourceLimits,
                                       get_role_spec, model_satisfies,
                                       validate_result)
from forge.models.capabilities import ALL_CAPABILITIES

EXPECTED_ROLES = {
    "planner", "architect", "coder", "tester", "debugger", "reviewer",
    "security", "performance", "researcher", "documentation",
}


def test_exactly_the_ten_canonical_roles_exist():
    assert {spec.role for spec in AGENT_ROLES} == EXPECTED_ROLES
    assert len(AGENT_ROLES) == 10


def test_every_role_has_a_complete_specification():
    for spec in AGENT_ROLES:
        # capability
        assert spec.capabilities, spec.role
        for capability in spec.capabilities:
            assert capability in ALL_CAPABILITIES, (spec.role, capability)
        # permissions
        assert spec.permissions, f"{spec.role} has no permissions"
        for resource, operation, scope in spec.permissions:
            assert resource and operation, spec.role
        # model requirement is a subset of the role's capabilities
        for capability in spec.model_requirement:
            assert capability in spec.capabilities, spec.role
        # task scope
        assert spec.task_scope, f"{spec.role} has no task scope"
        assert spec.task_scope <= {"sequential", "parallel", "dependent"}
        # resource limits
        assert isinstance(spec.limits, RoleResourceLimits)
        assert spec.limits.max_output_bytes >= 1
        assert spec.limits.max_duration_seconds > 0
        # timeout
        assert spec.timeout > 0, spec.role
        # result schema
        assert spec.result_schema, f"{spec.role} has no result schema"
        for name, field_spec in spec.result_schema.items():
            assert name.isidentifier(), (spec.role, name)
            for part in field_spec.split("|"):
                assert part in {"str", "int", "float", "bool", "list",
                                "dict", "any"}, (spec.role, field_spec)


def test_roles_with_write_permission_declare_a_scoped_write():
    writer_roles = {
        spec.role for spec in AGENT_ROLES
        for resource, operation, _ in spec.permissions
        if resource == "filesystem" and operation == "write"
    }
    assert "coder" in writer_roles
    assert "debugger" in writer_roles
    assert "planner" not in writer_roles
    assert "researcher" not in writer_roles


def test_read_only_roles_cannot_write():
    for role in ("planner", "architect", "researcher", "tester",
                 "reviewer", "security", "performance"):
        spec = get_role_spec(role)
        for resource, operation, _ in spec.permissions:
            assert not (resource == "filesystem"
                        and operation == "write"), role


def test_result_schema_accepts_conforming_results():
    assert validate_result(PLANNER, {
        "plan": ["step"], "requirements": "req", "risks": [],
        "summary": "ok"}) == []
    # optional fields may be absent
    assert validate_result(PLANNER, {
        "plan": [], "requirements": []}) == []
    # union type: requirements accepts str or list
    assert validate_result(PLANNER, {
        "plan": [], "requirements": ["a", "b"]}) == []


def test_result_schema_rejects_violations():
    violations = validate_result(CODER, {"summary": "no files field"})
    assert any("files" in v for v in violations)
    violations = validate_result(CODER, {
        "files": "not-a-list", "summary": "x"})
    assert any("files" in v for v in violations)
    # bool is not an int
    from forge.orchestration.roles import TESTER
    violations = validate_result(TESTER, {
        "passed": True, "tests_run": True, "failures": []})
    assert any("tests_run" in v for v in violations)
    # non-dict results violate a schema
    assert validate_result(CODER, "just text")


def test_result_schema_accepts_free_text_for_schemaless_roles():
    # Roles without a schema accept text output.
    empty = RoleResourceLimits()
    from forge.orchestration.roles import AgentRoleSpec
    spec = AgentRoleSpec(role="custom", display="Custom",
                         capabilities=("reasoning",),
                         permissions=(("filesystem", "read", "**"),),
                         model_requirement=(), limits=empty,
                         result_schema={})
    assert validate_result(spec, "free text") == []


def test_model_requirement_check_fails_closed():
    assert model_satisfies(PLANNER,
                           ("planning", "reasoning", "coding")) is True
    assert model_satisfies(PLANNER, ("coding",)) is False
    # mapping form: capability -> availability
    assert model_satisfies(PLANNER,
                           {"planning": True, "reasoning": False}) is False
    assert model_satisfies(PLANNER,
                           {"planning": True, "reasoning": True}) is True
    from forge.orchestration.roles import PERFORMANCE
    # no requirement: any model satisfies
    assert model_satisfies(PERFORMANCE, ()) is True


def test_unknown_role_raises():
    with pytest.raises(KeyError):
        get_role_spec("wizard")


def test_bad_role_specification_is_rejected():
    from forge.orchestration.roles import AgentRoleSpec

    def make(**overrides):
        base = dict(role="x", display="X",
                    capabilities=("coding",),
                    permissions=(("filesystem", "read", "**"),),
                    model_requirement=())
        base.update(overrides)
        return AgentRoleSpec(**base)

    with pytest.raises(ValueError):
        make(capabilities=("not-a-capability",))
    with pytest.raises(ValueError):
        make(model_requirement=("review",))  # not in capabilities
    with pytest.raises(ValueError):
        make(timeout=-1)
    with pytest.raises(ValueError):
        make(result_schema={"f": "banana"})
    with pytest.raises(ValueError):
        make(task_scope=frozenset())
