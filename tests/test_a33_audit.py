"""Permission audit log tests (A33)."""
import json

import pytest

from forge.security.audit import AuditLog, PermissionAuditEvent
from forge.security.policy import (
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
    Resource,
)
from forge.security.policy_gate import PolicyDecision


def test_event_carries_full_observability_shape():
    event = PermissionAuditEvent(
        agent="coder", resource="filesystem", operation="write",
        scope="src/a.py", risk="LOW", decision=PolicyDecision.ALLOW,
        matched_rule=("r1",), task_id="t", trace_id="trace",
        request_id="req", reason="rule r1")
    payload = event.to_dict()
    assert payload["decision"] == "ALLOW"
    assert payload["matched_rule"] == ["r1"]
    assert payload["task_id"] == "t"
    assert payload["trace_id"] == "trace"
    assert payload["request_id"] == "req"
    assert payload["timestamp"] > 0
    assert payload["approval_required"] is False


@pytest.mark.parametrize("secret", [
    "AKIAIOSFODNN7EXAMPLE",
    "-----BEGIN RSA PRIVATE KEY-----",
    "password = 'hunter2-hunter2'",
    "api_key: \"abcdef1234567890\"",
    "Bearer abcdefghijklmnopqrstuvwxyz1234",
])
def test_secrets_redacted_in_events(secret):
    event = PermissionAuditEvent(
        agent="coder", resource="filesystem", operation="write",
        scope=f"note {secret}", decision="ALLOW", reason=f"saw {secret}")
    payload = event.to_dict()
    assert secret not in json.dumps(payload)
    assert "REDACTED" in payload["scope"]
    assert "REDACTED" in payload["reason"]


def test_record_evaluation_from_engine_types():
    policy = PermissionPolicy(rules=[
        PermissionRule("r1", Resource.GIT, "commit",
                       PolicyDecision.REQUIRE_APPROVAL, reason="gate")])
    request = PermissionRequest(agent="coder", resource=Resource.GIT,
                                operation="commit", task_id="t")
    evaluation = policy.evaluate(request)
    log = AuditLog()
    event = log.record_evaluation(request, evaluation)
    assert len(log) == 1
    assert event.matched_rule == ("r1",)
    assert event.approval_required is True
    assert event.decision == PolicyDecision.REQUIRE_APPROVAL


def test_record_rejects_foreign_values():
    with pytest.raises(ValueError):
        AuditLog().record({"not": "an event"})


def test_query_filters():
    log = AuditLog()
    log.record_decision(agent="coder", resource="filesystem", operation="write",
                        decision="ALLOW", task_id="t1")
    log.record_decision(agent="browser", resource="browser", operation="navigate",
                        decision="DENY", task_id="t1")
    log.record_decision(agent="coder", resource="filesystem", operation="delete",
                        decision="DENY", task_id="t2")
    assert len(log.query(task_id="t1")) == 2
    assert len(log.query(decision=PolicyDecision.DENY)) == 2
    assert len(log.query(decision="deny")) == 2
    assert len(log.query(agent="coder")) == 2
    assert len(log.query(resource="browser")) == 1
    assert len(log.query(task_id="t2", decision="DENY")) == 1


def test_jsonl_sink_and_sink_errors(tmp_path):
    sink = tmp_path / "audit.jsonl"
    log = AuditLog(sink_path=sink)
    log.record_decision(agent="coder", resource="git", operation="commit",
                        decision="ALLOW")
    lines = sink.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["decision"] == "ALLOW"

    bad = AuditLog(sink_path=tmp_path / "missing-dir" / "audit.jsonl")
    bad.record_decision(agent="a", resource="git", operation="status",
                        decision="ALLOW")
    assert bad.sink_errors == 1
    assert len(bad) == 1  # in-memory log still authoritative


def test_events_are_immutable_snapshots():
    log = AuditLog()
    log.record_decision(agent="a", resource="git", operation="status",
                        decision="ALLOW")
    with pytest.raises(AttributeError):
        log.events[0].decision = PolicyDecision.DENY  # type: ignore[misc]
    assert isinstance(log.events, tuple)
