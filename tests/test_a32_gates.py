"""Review policy and security verification tests (A32.5/A32.6 rebuild).

The independent review gate applies a configurable MEDIUM budget while HIGH
and CRITICAL always block. Security verification executes real checks over
secret patterns, credential/key files, unsafe paths, dangerous commands, and
protected repository files — with no hardcoded scores.
"""
from forge.security.review import (
    FindingSeverity,
    ReviewFinding,
    ReviewGate,
    ReviewPolicy,
    ReviewVerdict,
)
from forge.security.verification import VerificationPipeline


def _medium(message="needs attention"):
    return ReviewFinding(FindingSeverity.MEDIUM, message, file="app.py",
                         rule="model-review")


# -- review policy --------------------------------------------------------

def test_default_policy_requests_changes_on_any_medium(tmp_path):
    decision = ReviewGate(tmp_path).review(
        diff="+def ok(): return 1\n", changed_files=["app.py"],
        model_findings=[_medium()])
    assert decision.verdict == ReviewVerdict.REQUEST_CHANGES
    assert not decision.approved
    assert decision.findings


def test_configured_medium_budget_approves_within_limit(tmp_path):
    gate = ReviewGate(tmp_path, policy=ReviewPolicy(max_medium_allowed=2))
    decision = gate.review(
        diff="+def ok(): return 1\n", changed_files=["app.py"],
        model_findings=[_medium("one")])
    assert decision.verdict == ReviewVerdict.APPROVE
    assert decision.approved
    assert len(decision.findings) == 1  # recorded, not hidden


def test_medium_over_budget_requests_changes(tmp_path):
    gate = ReviewGate(tmp_path, policy=ReviewPolicy(max_medium_allowed=2))
    decision = gate.review(
        diff="+def ok(): return 1\n", changed_files=["app.py"],
        model_findings=[_medium("one"), _medium("two"), _medium("three")])
    assert decision.verdict == ReviewVerdict.REQUEST_CHANGES
    assert not decision.approved


def test_high_blocks_despite_generous_medium_budget(tmp_path):
    gate = ReviewGate(tmp_path, policy=ReviewPolicy(max_medium_allowed=10))
    decision = gate.review(
        diff="+value = eval('1')\n", changed_files=["app.py"])
    assert decision.verdict == ReviewVerdict.BLOCK


def test_policy_is_recorded_in_decision(tmp_path):
    decision = ReviewGate(tmp_path).review(
        diff="+def ok(): return 1\n", changed_files=["app.py"])
    assert decision.to_dict()["policy"] == {"max_medium_allowed": 0}


# -- security verification --------------------------------------------------

def test_security_flags_private_key_files_on_sight(tmp_path):
    (tmp_path / "deploy.pem").write_text("placeholder\n")
    gate = VerificationPipeline(tmp_path).security(["deploy.pem"])
    assert not gate.passed
    assert any(f["rule"] == "private key material" for f in gate.evidence["findings"])


def test_security_flags_credential_data_files(tmp_path):
    (tmp_path / "credentials.json").write_text("{}\n")
    gate = VerificationPipeline(tmp_path).security(["credentials.json"])
    assert not gate.passed
    assert any(f["rule"] == "credential file" for f in gate.evidence["findings"])


def test_security_does_not_flag_source_named_credentials(tmp_path):
    (tmp_path / "credentials.py").write_text("def load(): return {}\n")
    (tmp_path / "mytokenizer.py").write_text("def tokens(): return []\n")
    gate = VerificationPipeline(tmp_path).security(
        ["credentials.py", "mytokenizer.py"])
    assert gate.passed, gate.evidence["findings"]


def test_security_flags_protected_declared_paths(tmp_path):
    gate = VerificationPipeline(tmp_path).security(
        [".forge/state.json", ".git/config"])
    assert not gate.passed
    assert any(f["rule"] == "protected repository file"
               for f in gate.evidence["findings"])


def test_security_flags_traversal_and_absolute_paths(tmp_path):
    gate = VerificationPipeline(tmp_path).security(["../outside.py", "/etc/passwd"])
    assert not gate.passed
    assert any(f["rule"] == "unsafe path" for f in gate.evidence["findings"])


def test_security_flags_shell_and_popen_usage(tmp_path):
    (tmp_path / "app.py").write_text(
        "import os\nos.popen('ls')\n")
    (tmp_path / "deploy.sh").write_text("#!/bin/sh\nbash -c 'echo hi'\n")
    gate = VerificationPipeline(tmp_path).security(["app.py", "deploy.sh"])
    assert not gate.passed
    assert len(gate.evidence["findings"]) >= 2


def test_security_clean_candidate_passes(tmp_path):
    (tmp_path / "app.py").write_text("def health(): return True\n")
    gate = VerificationPipeline(tmp_path).security(["app.py"])
    assert gate.passed
    assert gate.evidence["findings"] == []
