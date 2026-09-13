"""A83 — permission boundaries: grants, ceilings, and the no-self-grant rule.

These are the security invariants of the engine. Each one is checked by
driving the real classes the runtime uses, not a copy of their logic.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a83 import make_engine, make_project  # noqa: E402

from forge.agents.engine import (  # noqa: E402
    FORBIDDEN_OPERATIONS,
    AgentPermissionError,
    GrantLedger,
    spec_from_template,
)
from forge.security.permissions import OperationMode, PermissionLevel  # noqa: E402


@pytest.fixture()
def engine(tmp_path):
    make_project(tmp_path / "demo")
    return make_engine(tmp_path / "demo")


def _ledger(spec):
    return GrantLedger(spec.name, spec, {"grants": [], "revocations": []})


# -- no self-grant -------------------------------------------------------


def test_agent_cannot_grant_permissions_to_itself():
    spec = spec_from_template("coding", name="exporter")
    ledger = _ledger(spec)
    with pytest.raises(AgentPermissionError) as info:
        ledger.grant("write_file", actor="exporter")
    assert "cannot grant permissions to itself" in str(info.value)
    assert ledger.active_operations() == ()
    assert ledger.refusals, "the attempt must be recorded"
    assert ledger.refusals[-1]["operation"] == "write_file"


@pytest.mark.parametrize("alias", ["exporter", "EXPORTER", " exporter ",
                                   "agent:exporter", "agent-exporter"])
def test_self_grant_aliases_are_also_refused(alias):
    spec = spec_from_template("coding", name="exporter")
    ledger = _ledger(spec)
    with pytest.raises(AgentPermissionError):
        ledger.grant("write_file", actor=alias)


def test_anonymous_grants_are_refused():
    spec = spec_from_template("coding", name="exporter")
    ledger = _ledger(spec)
    with pytest.raises(AgentPermissionError) as info:
        ledger.grant("write_file", actor="   ")
    assert "named operator" in str(info.value)


def test_operator_grants_are_recorded_with_provenance():
    spec = spec_from_template("coding", name="exporter")
    ledger = _ledger(spec)
    grant = ledger.grant("write_file", actor="alice", reason="incident 7")
    assert grant.granted_by == "alice"
    assert grant.reason == "incident 7"
    assert ledger.active_operations() == ("write_file",)
    assert ledger.refusals == []


# -- the ceiling ---------------------------------------------------------


def test_grants_cannot_exceed_the_spec_ceiling():
    spec = spec_from_template("research", name="scout")
    ledger = _ledger(spec)
    assert "write_file" not in spec.permissions.operations
    with pytest.raises(AgentPermissionError) as info:
        ledger.grant("write_file", actor="alice")
    assert "outside the declared ceiling" in str(info.value)
    assert ledger.active_operations() == ()


@pytest.mark.parametrize("operation", sorted(FORBIDDEN_OPERATIONS))
def test_blocked_operations_cannot_be_granted_to_anyone(engine, operation):
    engine.create_from_template("coding", "exporter", actor="alice")
    with pytest.raises(AgentPermissionError) as info:
        engine.grant("exporter", operation, actor="alice")
    assert "permanently blocked" in str(info.value)


def test_unknown_operations_are_refused():
    spec = spec_from_template("coding", name="exporter")
    ledger = _ledger(spec)
    with pytest.raises(AgentPermissionError):
        ledger.grant("sudo_everything", actor="alice")


def test_granting_an_undeclared_operation_is_refused_by_the_engine(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    with pytest.raises(AgentPermissionError):
        engine.grant("exporter", "git_push", actor="alice")
    assert engine.permissions("exporter")["granted"] == []


# -- revocation ----------------------------------------------------------


def test_revocation_is_recorded_and_effective(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    engine.grant_spec("exporter", actor="alice")
    assert "write_file" in engine.permissions("exporter")["granted"]
    engine.revoke("exporter", "write_file", actor="alice", reason="rollback")
    permissions = engine.permissions("exporter")
    assert "write_file" not in permissions["granted"]
    assert "write_file" in permissions["not_granted"]
    ledger = engine.factory.grants("exporter")
    assert any(entry["operation"] == "write_file"
               and entry["by"] == "alice"
               for entry in ledger["revocations"])


def test_revoking_an_ungranted_operation_is_refused(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    with pytest.raises(AgentPermissionError):
        engine.revoke("exporter", "write_file", actor="alice")


def test_revocation_beats_the_grant_it_follows(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    engine.grant("exporter", "write_file", actor="alice")
    engine.revoke("exporter", "write_file", actor="alice")
    assert "write_file" not in engine.permissions("exporter")["granted"]
    # Only a newer operator grant restores access; the agent still cannot
    # grant anything to itself.
    engine.grant("exporter", "write_file", actor="bob", reason="restored")
    assert "write_file" in engine.permissions("exporter")["granted"]
    ledger = engine.factory.grants("exporter")
    assert len(ledger["grants"]) == 2
    assert len(ledger["revocations"]) == 1
    with pytest.raises(AgentPermissionError):
        engine.factory._ledger(engine.get("exporter")).grant(
            "read_file", actor="exporter")


# -- expiring grants -----------------------------------------------------


def test_time_bounded_grants_expire():
    spec = spec_from_template("coding", name="exporter")
    ledger = _ledger(spec)
    ledger.grant("write_file", actor="alice", ttl_seconds=60.0, now=1000.0)
    assert ledger.holds("write_file", now=1030.0) is True
    assert ledger.holds("write_file", now=1060.0) is False
    assert ledger.holds("write_file", now=1100.0) is False
    permanent = _ledger(spec)
    permanent.grant("write_file", actor="alice")
    assert permanent.holds("write_file", now=10 ** 9) is True


# -- the runtime's permission view ---------------------------------------


def test_ungranted_operations_are_blocked_in_the_agent_permission_view(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    engine.validate("exporter", actor="alice")
    package = engine.get("exporter")
    ledger = GrantLedger(package.name, package.spec,
                         engine.store.read_grants("exporter"))
    manager = engine.runtime.agent_manager(package, ledger)
    for operation in ("write_file", "read_file", "delete_file", "run_command",
                      "git_commit", "git_push"):
        assert manager.rules[operation] == PermissionLevel.BLOCKED, operation
    for operation in ("delete_repository", "expose_secrets"):
        assert manager.rules[operation] == PermissionLevel.BLOCKED

    engine.grant("exporter", "read_file", actor="alice")
    ledger = GrantLedger(package.name, package.spec,
                         engine.store.read_grants("exporter"))
    manager = engine.runtime.agent_manager(package, ledger)
    assert manager.rules["read_file"] == PermissionLevel.SAFE
    assert manager.rules["write_file"] == PermissionLevel.BLOCKED


def test_mode_ceiling_only_tightens_the_session_mode(engine):
    from forge.agents.engine import strict_mode_for

    class Session:
        def __init__(self, mode):
            self.mode = OperationMode(mode)

    spec = spec_from_template("research", name="scout")
    assert spec.permissions.mode_ceiling == "safe"
    assert strict_mode_for(spec, Session("autonomous")) == OperationMode.SAFE
    assert strict_mode_for(spec, Session("safe")) == OperationMode.SAFE
    assert strict_mode_for(spec, Session("locked")) == OperationMode.LOCKED

    coding = spec_from_template("coding", name="exporter")
    assert strict_mode_for(coding, Session("autonomous")) == \
        OperationMode.ASSISTED
    assert strict_mode_for(coding, Session("locked")) == OperationMode.LOCKED


# -- effective permissions ----------------------------------------------


def test_effective_permissions_show_ceiling_grants_and_blocks(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    engine.grant("exporter", "read_file", actor="alice")
    permissions = engine.permissions("exporter")
    assert permissions["granted"] == ["read_file"]
    assert set(permissions["not_granted"]) == {
        "search_files", "git_status", "run_tests", "write_file"}
    assert permissions["blocked_always"] == list(FORBIDDEN_OPERATIONS)
    assert permissions["denied_paths"] == ["secrets/"]


def test_a_stale_grant_above_a_lowered_ceiling_is_inert(engine):
    from forge.agents.engine import spec_from_template

    engine.create_from_template("coding", "exporter", actor="alice")
    engine.grant("exporter", "read_file", actor="alice")
    engine.grant("exporter", "write_file", actor="alice")
    assert set(engine.permissions("exporter")["granted"]) == {
        "read_file", "write_file"}
    # A *minor* change keeps the ledger, but if the ceiling no longer
    # contains the operation the grant must stop working.
    package = engine.get("exporter")
    narrowed = spec_from_template(
        "coding", name="exporter",
        overrides={"tools": [{"name": "read_file"}, {"name": "search"}],
                   "permissions": {"operations": ["read_file",
                                                  "search_files"],
                                   "mode_ceiling": "safe"},
                   "verification": {"require_tests": False}})
    package.spec = narrowed
    package.version = "1.1.0"
    engine.store.write_manifest(package)
    ledger = GrantLedger("exporter", narrowed,
                         engine.store.read_grants("exporter"))
    assert ledger.active_operations() == ("read_file",)
    assert ledger.holds("write_file") is False, \
        "a grant above the new ceiling must go inert, not carry over"
    assert ledger.holds("search_files") is False, \
        "the ceiling alone grants nothing"
