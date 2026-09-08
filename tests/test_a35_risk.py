"""A35 desktop risk classification and hard-invariant tests.

Hard invariants are deterministic always-DENY rules: no profile, policy
rule, or approval token can override them.
"""
from __future__ import annotations

import pytest

from forge.desktop.actions import DesktopRequest
from forge.desktop.risk import (
    BASE_RISK,
    RiskAssessment,
    classify,
    hard_violations,
)


def req(action, target="", params=None, agent="tester"):
    return DesktopRequest(action, target=target, params=params or {},
                          agent=agent)


def test_base_risks_are_ordered_sensibly():
    assert BASE_RISK["screenshot"] == "NONE"
    assert BASE_RISK["system_info"] == "NONE"
    assert BASE_RISK["window_list"] == "NONE"
    assert BASE_RISK["mouse_move"] == "LOW"
    assert BASE_RISK["mouse_click"] == "MEDIUM"
    assert BASE_RISK["keyboard"] == "MEDIUM"
    assert BASE_RISK["launch"] == "MEDIUM"


def test_parameter_raises():
    assert classify(req("launch", "notepad",
                        params={"args": ["--x"]})).risk == "HIGH"
    assert classify(req("window", "calc",
                        params={"op": "close"})).risk == "MEDIUM"
    assert classify(req("clipboard", params={"op": "write",
                                             "content": "x"})).risk \
        == "MEDIUM"
    assert classify(req("file_access", "a.txt",
                        params={"mode": "delete"})).risk == "HIGH"
    assert classify(req("keyboard", params={"keys": ["ctrl+alt+del"]})).risk \
        == "CRITICAL"


def test_unknown_risk_fails_safe():
    # classify is internal; unknown actions never reach it (validation
    # rejects them first), but the rank helper degrades to MEDIUM.
    from forge.desktop.risk import risk_rank
    assert risk_rank("BOGUS") == 2


@pytest.mark.parametrize("action,target,params,fragment", [
    # credential extraction
    ("launch", "bitwarden", {}, "credential"),
    ("keyboard", "", {"text": "open lastpass vault"}, "credential"),
    ("file_access", "id_rsa", {"mode": "read"}, "credential"),
    ("file_access", ".ssh/config", {"mode": "read"}, "credential"),
    ("launch", "aws", {"args": ["cat", ".aws/credentials"]}, "credential"),
    # security-control disabling
    ("launch", "defender", {"args": ["kill", "-9"]}, "security"),
    ("window", "firewall", {"op": "close"}, "security"),
    ("process", "defender", {"op": "terminate"}, "security"),
    ("app_action", "avast", {"action": "close_window"}, "security"),
    # privilege escalation
    ("launch", "sudo", {"args": ["whoami"]}, "privilege escalation"),
    ("keyboard", "", {"text": "su - root"}, "privilege escalation"),
    ("launch", "pkexec", {}, "privilege escalation"),
    # unauthorized persistence
    ("file_access", ".config/autostart/x.desktop",
     {"mode": "write", "content": "x"}, "persistence"),
    ("launch", "crontab", {"args": ["-e"]}, "persistence"),
    ("launch", "schtasks", {"args": ["/create"]}, "persistence"),
    # unauthorized remote control
    ("launch", "vncserver", {}, "remote control"),
    ("launch", "ngrok", {}, "remote control"),
    ("launch", "xrdp", {}, "remote control"),
])
def test_hard_invariants_always_deny(action, target, params, fragment):
    violations = hard_violations(req(action, target, params))
    assert violations, (action, target, params)
    assert any(fragment in violation for violation in violations)
    assessment = classify(req(action, target, params))
    assert assessment.forbidden
    assert isinstance(assessment, RiskAssessment)


def test_benign_requests_have_no_violations():
    assert not hard_violations(req("screenshot"))
    assert not hard_violations(req("launch", "notepad"))
    assert not hard_violations(req("launch", "calculator",
                                   params={"args": ["1+1"]}))
    assert not hard_violations(req("keyboard", "notepad",
                                   params={"text": "hello"}))
    assert not hard_violations(req("clipboard", params={"op": "read"}))
    # launching security software itself is allowed
    assert not hard_violations(req("launch", "defender"))


def test_violations_are_deduplicated():
    request = req("launch", "defender", params={"args": ["sudo", "kill"]})
    violations = hard_violations(request)
    assert len(violations) == len(set(violations))
    assert any("privilege escalation" in item for item in violations)
    assert any("security control" in item for item in violations)
