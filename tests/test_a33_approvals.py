"""Approval requests, tokens, and task grants (A33)."""
import pytest

from forge.security.approvals import (
    ApprovalRequest,
    ApprovalStatus,
    ApprovalStore,
    TaskGrant,
)
from forge.security.policy import PermissionRequest, Resource, validate_scope


def _store(start=1000.0):
    box = {"t": float(start)}
    return ApprovalStore(clock=lambda: box["t"]), box


def _request(**kwargs):
    values = {"agent": "coder", "resource": Resource.FILESYSTEM,
              "operation": "write", "scopes": ("src/app.py",)}
    values.update(kwargs)
    return ApprovalRequest(**values)


def _approved(store, **kwargs):
    request = store.submit(_request(**kwargs))
    store.decide(request.id, True, "operator")
    return request


# -- request validation ---------------------------------------------------------

def test_request_validates_operation_agent_and_scopes():
    with pytest.raises(ValueError):
        _request(operation="teleport")
    with pytest.raises(ValueError):
        _request(agent="")
    with pytest.raises(ValueError):
        _request(resource=Resource.TERMINAL, operation="execute", scopes=("**",))
    with pytest.raises(ValueError):
        _request(scopes=("../escape.py",))


def test_validate_scope_normalizes_and_rejects():
    assert validate_scope(Resource.FILESYSTEM, "gen/") == "gen/**"
    assert validate_scope(Resource.BROWSER, "Example.COM") == "example.com"
    with pytest.raises(ValueError):
        validate_scope(Resource.FILESYSTEM, "")
    with pytest.raises(ValueError):
        validate_scope(Resource.TERMINAL, "**")
    with pytest.raises(ValueError):
        validate_scope(Resource.NETWORK, "*.com")


def test_describe_explains_the_request():
    request = _request(task_id="t1", risk="low", reason="Add CSV export",
                       files=("src/export.py",), model="q", provider="ollama",
                       consequences="Two files change.")
    text = request.describe()
    for expected in ("APPROVAL REQUIRED", "coder", "write (filesystem)",
                     "src/app.py", "src/export.py", "LOW", "Add CSV export",
                     "q (ollama)", "Two files change.", request.id):
        assert expected in text


def test_request_serializes():
    assert _request().to_dict()["status"] == "pending"


# -- decide ---------------------------------------------------------------------

def test_pending_lists_only_live_pending_requests():
    store, box = _store()
    first = store.submit(_request())
    store.submit(_request())
    assert len(store.pending()) == 2
    store.decide(first.id, True, "operator")
    assert len(store.pending()) == 1
    box["t"] += 100000.0
    assert store.pending() == []


def test_agent_cannot_approve_its_own_request():
    store, _ = _store()
    request = store.submit(_request(agent="coder"))
    with pytest.raises(ValueError, match="cannot approve its own"):
        store.decide(request.id, True, "coder")


def test_decide_rejects_unknown_double_and_expired():
    store, box = _store()
    with pytest.raises(ValueError, match="Unknown"):
        store.decide("missing", True, "operator")
    request = store.submit(_request())
    store.decide(request.id, False, "operator")
    assert request.status == ApprovalStatus.DENIED
    with pytest.raises(ValueError, match="already denied"):
        store.decide(request.id, True, "operator")
    stale = store.submit(_request())
    box["t"] += 100000.0
    with pytest.raises(ValueError, match="expired"):
        store.decide(stale.id, True, "operator")
    assert stale.status == ApprovalStatus.EXPIRED


# -- issue ------------------------------------------------------------------------

def test_issue_requires_approved_request_and_recorded_approver():
    store, _ = _store()
    pending = store.submit(_request())
    with pytest.raises(ValueError, match="not approved"):
        store.issue(pending.id, "operator")
    request = _approved(store)
    with pytest.raises(ValueError, match="recorded approver"):
        store.issue(request.id, "someone-else")
    with pytest.raises(ValueError):
        store.issue(request.id, "operator", max_uses=0)
    with pytest.raises(ValueError):
        store.issue(request.id, "operator", ttl_seconds=0)
    token = store.issue(request.id, "operator", ttl_seconds=60,
                        fingerprint="fp", bind=(("port", 443),))
    assert token.agent == "coder"
    assert token.issued_by == "operator"
    assert token.expires_at == token.issued_at + 60
    assert token.fingerprint == "fp"
    assert token.to_dict()["max_uses"] == 1


# -- redeem -------------------------------------------------------------------------

def _permission(agent="coder", resource=Resource.FILESYSTEM,
                operation="write", scope="src/app.py", **kwargs):
    return PermissionRequest(agent=agent, resource=resource,
                             operation=operation, scope=scope, **kwargs)


def test_redeem_consumes_single_use_token():
    store, _ = _store()
    request = _approved(store)
    token = store.issue(request.id, "operator")
    allowed, reason = store.redeem(token.id, _permission())
    assert allowed and "operator" in reason
    allowed, reason = store.redeem(token.id, _permission())
    assert not allowed and "already been used" in reason


def test_multi_use_token_honors_its_limit():
    store, _ = _store()
    request = _approved(store, scopes=("src/**",))
    token = store.issue(request.id, "operator", max_uses=2)
    assert store.redeem(token.id, _permission())[0] is True
    assert store.redeem(token.id, _permission())[0] is True
    assert store.redeem(token.id, _permission())[0] is False


def test_redeem_rejects_unknown_revoked_and_expired():
    store, box = _store()
    allowed, reason = store.redeem("missing", _permission())
    assert not allowed and "Unknown" in reason
    request = _approved(store)
    token = store.issue(request.id, "operator", ttl_seconds=60)
    assert store.revoke_token(token.id) is True
    assert store.redeem(token.id, _permission())[0] is False
    assert store.revoke_token("missing") is False
    fresh = store.issue(request.id, "operator", ttl_seconds=60)
    box["t"] += 61.0
    allowed, reason = store.redeem(fresh.id, _permission())
    assert not allowed and "expired" in reason


def test_token_is_non_transferable_and_task_bound():
    store, _ = _store()
    request = _approved(store, task_id="t1")
    token = store.issue(request.id, "operator")
    allowed, reason = store.redeem(
        token.id, _permission(agent="intruder", task_id="t1"))
    assert not allowed and "another agent" in reason
    allowed, reason = store.redeem(token.id, _permission(task_id="t2"))
    assert not allowed and "another task" in reason


def test_token_cannot_expand_operation_or_scope():
    store, _ = _store()
    request = _approved(store, scopes=("src/foo.py",))
    token = store.issue(request.id, "operator", max_uses=5)
    allowed, _ = store.redeem(
        token.id, _permission(operation="delete", scope="src/foo.py"))
    assert not allowed
    allowed, _ = store.redeem(token.id, _permission(scope="src/other.py"))
    assert not allowed
    allowed, _ = store.redeem(token.id, _permission(scope="/etc/x"))
    assert not allowed
    allowed, _ = store.redeem(token.id, _permission())
    assert not allowed  # src/app.py != src/foo.py
    assert store.redeem(token.id, _permission(scope="src/foo.py"))[0] is True


def test_token_files_binding_is_exact():
    store, _ = _store()
    request = _approved(store, scopes=("src/**",), files=("src/a.py",))
    token = store.issue(request.id, "operator", max_uses=2)
    allowed, reason = store.redeem(token.id, _permission(scope="src/b.py"))
    assert not allowed and "does not cover this file" in reason
    assert store.redeem(token.id, _permission(scope="src/a.py"))[0] is True


def test_token_fingerprint_and_bindings_enforced():
    store, _ = _store()
    request = _approved(store, scopes=("src/**",))
    token = store.issue(request.id, "operator", max_uses=4,
                        fingerprint="fp1", bind=(("port", 443),))
    permission = _permission(scope="src/a.py",
                             details=(("port", 443),))
    allowed, reason = store.redeem(token.id, permission)
    assert not allowed and "different proposal" in reason
    allowed, reason = store.redeem(
        token.id, _permission(scope="src/a.py",
                              details=(("port", 80),)), fingerprint="fp1")
    assert not allowed and "binding 'port'" in reason
    assert store.redeem(token.id, permission, fingerprint="fp1")[0] is True


def test_scope_containment_by_resource():
    store, _ = _store()
    fs = _approved(store, agent="a", task_id="t",
                   scopes=("src/**",))
    fs_token = store.issue(fs.id, "operator", max_uses=2)
    assert store.redeem(fs_token.id, _permission(
        agent="a", task_id="t", scope="src/deep/x.py"))[0] is True

    browser = _approved(store, agent="a", task_id="t",
                        resource=Resource.BROWSER, operation="navigate",
                        scopes=("example.com",))
    browser_token = store.issue(browser.id, "operator")
    good = PermissionRequest(agent="a", task_id="t",
                             resource=Resource.BROWSER, operation="navigate",
                             scope="https://example.com/x")
    assert store.redeem(browser_token.id, good)[0] is True
    evil = PermissionRequest(agent="a", task_id="t",
                             resource=Resource.BROWSER, operation="navigate",
                             scope="https://evil.example/x")
    assert store.redeem(browser_token.id, evil)[0] is False

    terminal = _approved(store, agent="a", task_id="t",
                         resource=Resource.TERMINAL, operation="execute",
                         scopes=("pytest",))
    terminal_token = store.issue(terminal.id, "operator", max_uses=2)
    exact = PermissionRequest(agent="a", task_id="t",
                              resource=Resource.TERMINAL, operation="execute",
                              scope="pytest")
    assert store.redeem(terminal_token.id, exact)[0] is True
    other = PermissionRequest(agent="a", task_id="t",
                              resource=Resource.TERMINAL, operation="execute",
                              scope="/usr/bin/pytest")
    assert store.redeem(terminal_token.id, other)[0] is False


# -- escalation -----------------------------------------------------------------------

def test_escalation_still_needs_another_approver():
    store, _ = _store()
    request = store.request_escalation(
        agent="coder", task_id="t", resource=Resource.FILESYSTEM,
        operation="delete", scopes=("tmp/old.py",),
        reason="Remove stale file", current="write src/**")
    assert request.escalation is True
    assert "exceeds" in request.consequences
    assert "escalation" in request.describe().lower()
    with pytest.raises(ValueError, match="cannot approve its own"):
        store.decide(request.id, True, "coder")
    store.decide(request.id, True, "operator")
    token = store.issue(request.id, "operator")
    assert token.operation == "delete"


# -- task grants --------------------------------------------------------------------------

def test_task_grant_covers_declared_files_only():
    store, box = _store()
    grant = store.grant_task("t1", ["a.py", "b.py"], "fp1", ttl_seconds=60)
    assert isinstance(grant, TaskGrant)
    assert store.check_task_grant("t1", "a.py", "fp1")[0] is True
    assert store.check_task_grant("t1", "c.py", "fp1")[0] is False
    assert store.check_task_grant("t1", "a.py", "other")[0] is False
    assert store.check_task_grant("t2", "a.py", "fp1")[0] is False
    assert grant.to_dict()["files"] == ["a.py", "b.py"]
    box["t"] += 61.0
    assert store.check_task_grant("t1", "a.py", "fp1")[0] is False
    with pytest.raises(ValueError):
        store.grant_task("", ["a.py"], "fp")
    with pytest.raises(ValueError):
        store.grant_task("t", ["a.py"], "fp", ttl_seconds=0)


def test_revoke_task_kills_tokens_and_grants():
    store, _ = _store()
    request = _approved(store, task_id="t1")
    token = store.issue(request.id, "operator")
    store.grant_task("t1", ["a.py"], "fp")
    assert store.revoke_task("t1") == 2
    assert store.revoke_task("t1") == 0
    assert store.redeem(token.id, _permission(task_id="t1"))[0] is False
    assert store.check_task_grant("t1", "a.py", "fp")[0] is False
    assert store.active_grants("t1") == []


def test_prune_drops_expired_without_extending():
    store, box = _store()
    request = _approved(store)
    store.issue(request.id, "operator", ttl_seconds=10)
    store.grant_task("t", ["a.py"], "fp", ttl_seconds=10)
    box["t"] += 11.0
    assert store.prune() == 2
    assert store.prune() == 0
