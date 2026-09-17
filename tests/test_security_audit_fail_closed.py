"""Slice D: security audit writes fail closed; ordinary records degrade visibly."""
from pathlib import Path

import pytest

from forge.security.audit import AuditLog, AuditSinkError
from forge.security.policy_gate import PolicyDecision
from forge.server.authorization import Authorizer
from forge.security.permissions import OperationMode


def test_security_audit_sink_failure_is_raised(tmp_path: Path) -> None:
    # A directory is deliberately an invalid append target and reliably causes
    # OSError without depending on filesystem permissions.
    log = AuditLog(tmp_path)
    with pytest.raises(AuditSinkError):
        log.record_decision(
            agent="forge-server",
            resource="filesystem",
            operation="write",
            decision=PolicyDecision.DENY,
            security_sensitive=True,
        )
    assert log.sink_errors == 1
    assert len(log) == 1


def test_ordinary_audit_sink_failure_remains_visible_but_non_fatal(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    event = log.record_decision(
        agent="telemetry",
        resource="model",
        operation="observe",
        decision=PolicyDecision.ALLOW,
    )
    assert event.decision == PolicyDecision.ALLOW
    assert log.sink_errors == 1
    assert len(log) == 1


def test_task_admission_does_not_swallow_security_audit_failure(tmp_path: Path) -> None:
    authorizer = Authorizer("safe")
    log = AuditLog(tmp_path)
    with pytest.raises(AuditSinkError):
        authorizer.ensure_task_permitted(
            "/project",
            OperationMode.SAFE,
            project_id="demo",
            audit=log,
        )
