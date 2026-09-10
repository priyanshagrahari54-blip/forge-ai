"""A50 training-data security tests (Phase 5).

Level 1: TrainingDataPolicy is fail-closed — secrets/credentials/PII
         are never uploaded automatically, external upload defaults to
         deny, and authorization cannot bypass a secret scan unless the
         operator mode explicitly allows it.
Level 2: the pipeline refuses policy-violating uploads before any API
         call (no OPENAI_API_KEY needed to observe the refusal).
"""
from __future__ import annotations

import os

import pytest

from forge.agents.training import (
    AgentTrainingPipeline,
    TrainingDataPolicy,
    TrainingDataset,
    scan_text,
)


@pytest.fixture(autouse=True)
def _policy_env(monkeypatch):
    monkeypatch.delenv("FORGE_TRAINING_EXTERNAL_UPLOAD", raising=False)


def _dataset(*payloads) -> TrainingDataset:
    dataset = TrainingDataset("worker")
    for payload in payloads:
        if isinstance(payload, tuple):
            text_in, text_out = payload
        else:
            text_in, text_out = payload, "generated patch: ok"
        dataset.add(text_in, text_out, success=True, task_id="t1")
    return dataset


# ---------------------------------------------------------------------------
# scan_text
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "api_key = 'abcdef1234567890'",
    "token = \"ghp_abcdefghijklmnopqrstuvwxyz1234567890AB\"",
    "sk-abcdefghijklmnopqrstuvwxyz123456",
    # Composed at runtime: a literal Slack-shaped token would trip GitHub
    # push protection; the product scanner must still flag the real string.
    "xox" + "b-123456789012-123456789012-abcdefghijklmnop",
    "AIzaSyA1234567890abcdefghijklmnopqrstuvwx",
    "AKIAIOSFODNN7EXAMPLE",
    "-----BEGIN RSA PRIVATE KEY-----\nabc",
    "https://user:supersecret@example.com/x",
    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
])
def test_secret_material_detected(text):
    scan = scan_text(text)
    assert scan["level"] == "secret"
    assert scan["secret_hits"], text
    assert scan["clean"] is False


@pytest.mark.parametrize("text", [
    "contact me at alice@example.com",
    "SSN 123-45-6789 on file",
])
def test_pii_detected(text):
    scan = scan_text(text)
    assert scan["level"] == "confidential"
    assert scan["pii_hits"], text


def test_clean_content_is_clean():
    scan = scan_text("def add(a, b):\n    return a + b\n")
    assert scan["level"] in ("internal", "public")
    assert scan["clean"] is True


# ---------------------------------------------------------------------------
# TrainingDataPolicy matrix (fail-closed)
# ---------------------------------------------------------------------------

def test_default_mode_is_deny():
    policy = TrainingDataPolicy()
    assert policy.mode == "deny"
    assert policy.to_dict()["fail_closed"] is True


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        TrainingDataPolicy(mode="maybe")


def test_empty_dataset_never_uploadable():
    decision = TrainingDataPolicy(mode="allow").evaluate(
        _dataset(), authorized=True)
    assert decision["allowed"] is False


def test_secret_never_uploaded_in_any_mode():
    dataset = _dataset(("task: fix login", "token = 'abcdef1234567890xyz'"))
    for mode in ("deny", "approval", "allow"):
        policy = TrainingDataPolicy(mode=mode)
        decision = policy.evaluate(dataset, authorized=True)
        assert decision["allowed"] is False
        assert decision["secret_present"] is True
        assert decision["denied"] == "secret"
        assert policy.evaluate(dataset, authorized=False)["allowed"] \
            is False
        # default (env-less) mode is deny:
        assert TrainingDataPolicy().evaluate(
            dataset, authorized=True)["allowed"] is False


def test_secret_in_input_also_blocked():
    dataset = _dataset(("use my key sk-abcdefghijklmnopqrstuvwxyz123456",
                        "ok"))
    # deny and approval modes refuse secrets even when authorized.
    for mode in ("deny", "approval"):
        decision = TrainingDataPolicy(mode=mode).evaluate(
            dataset, authorized=True)
        assert decision["allowed"] is False
        assert decision["secret_present"] is True
    # allow mode still refuses without explicit authorization…
    decision = TrainingDataPolicy(mode="allow").evaluate(
        dataset, authorized=False)
    assert decision["allowed"] is False
    # …and now ALSO refuses with the operator mode + authorization both
    # set: SECRET can never be overridden by a generic authorized flag.
    decision = TrainingDataPolicy(mode="allow").evaluate(
        dataset, authorized=True)
    assert decision["allowed"] is False
    assert decision["secret_present"] is True
    assert decision["denied"] == "secret"


def test_secret_with_every_permissive_signal_is_still_denied():
    """secret + allow + authorized + valid approval-equivalent → DENY."""
    dataset = _dataset(("task: deploy", "password = 's3cr3t-value-12345'"))
    decision = TrainingDataPolicy(mode="allow").evaluate(
        dataset, authorized=True)
    assert decision["allowed"] is False
    assert decision["denied"] == "secret"
    # The denial reason is redacted: it never echoes the secret text.
    assert "s3cr3t-value-12345" not in decision["reason"]
    assert "12345" not in decision["reason"]
    assert decision.get("redacted") is True


def test_secret_denial_reason_is_redacted():
    dataset = _dataset(("task: fix login",
                        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"))
    decision = TrainingDataPolicy(mode="allow").evaluate(
        dataset, authorized=True)
    assert decision["allowed"] is False
    low = decision["reason"].lower()
    assert "abcdefghijklmnopqrstuvwxyz123456" not in low
    assert "bearer" not in low
    assert decision.get("redacted") is True


def test_confidential_requires_explicit_approval():
    dataset = _dataset(("contact alice@example.com for the patch",
                        "ok"))
    policy = TrainingDataPolicy(mode="approval")
    # confidential + no approval → DENY
    denied = policy.evaluate(dataset, authorized=False)
    assert denied["allowed"] is False
    assert denied["report"]["confidential_or_pii"] == 1
    # confidential + explicit valid approval → allowed per policy
    allowed = policy.evaluate(dataset, authorized=True)
    assert allowed["allowed"] is True
    # deny mode never uploads, even for confidential content.
    assert TrainingDataPolicy(mode="deny").evaluate(
        dataset, authorized=True)["allowed"] is False
    # allow mode without authorization refuses confidential content.
    assert TrainingDataPolicy(mode="allow").evaluate(
        dataset, authorized=False)["allowed"] is False


def test_public_and_internal_follow_normal_policy_evaluation():
    public = _dataset("add CSV export to the reports page")
    internal = _dataset(("refactor module: build for release-2026",
                         "implemented per ADR-42"))
    # deny mode: everything refused.
    assert TrainingDataPolicy(mode="deny").evaluate(
        public, authorized=True)["allowed"] is False
    # approval mode: authorization is the gate.
    assert TrainingDataPolicy(mode="approval").evaluate(
        public, authorized=False)["allowed"] is False
    assert TrainingDataPolicy(mode="approval").evaluate(
        public, authorized=True)["allowed"] is True
    assert TrainingDataPolicy(mode="allow").evaluate(
        public, authorized=True)["allowed"] is True
    # internal content that scans clean behaves like public content.
    assert TrainingDataPolicy(mode="approval").evaluate(
        internal, authorized=True)["allowed"] is True
    for decision in (TrainingDataPolicy(mode="allow").evaluate(
            public, authorized=True),
            TrainingDataPolicy(mode="approval").evaluate(
                internal, authorized=True)):
        assert decision["allowed"] is True
        assert "secret_present" not in decision


def test_deny_mode_blocks_even_clean_data():
    dataset = _dataset("add CSV export")
    policy = TrainingDataPolicy(mode="deny")
    decision = policy.evaluate(dataset, authorized=True)
    assert decision["allowed"] is False
    assert "disabled" in decision["reason"]


def test_approval_mode_requires_authorization():
    dataset = _dataset("add CSV export")
    policy = TrainingDataPolicy(mode="approval")
    assert policy.evaluate(dataset, authorized=False)["allowed"] is False
    assert policy.evaluate(dataset, authorized=True)["allowed"] is True


def test_allow_mode_requires_authorization():
    dataset = _dataset("add CSV export")
    policy = TrainingDataPolicy(mode="allow")
    assert policy.evaluate(dataset, authorized=False)["allowed"] is False
    assert policy.evaluate(dataset, authorized=True)["allowed"] is True


def test_pii_blocks_upload_without_authorization():
    dataset = _dataset("email alice@example.com the patch")
    policy = TrainingDataPolicy(mode="approval")
    decision = policy.evaluate(dataset, authorized=False)
    assert decision["allowed"] is False
    assert decision["report"]["confidential_or_pii"] == 1
    assert policy.evaluate(dataset, authorized=True)["allowed"] is True


def test_scan_dataset_reports_indexes():
    dataset = _dataset("clean task one",
                       ("clean two", "sk-abcdefghijklmnopqrstuvwxyz123456"))
    report = TrainingDataPolicy(mode="approval").scan_dataset(dataset)
    assert report["examples"] == 2
    assert report["clean"] == 1
    assert report["secret"] == 1
    assert report["secret_example_indexes"] == [1]


# ---------------------------------------------------------------------------
# pipeline integration: policy refusal happens before any API call
# ---------------------------------------------------------------------------

def test_pipeline_refuses_secret_dataset_before_upload(monkeypatch):
    pipeline = AgentTrainingPipeline("session-1")
    runs = [{"requirement": "fix login",
             "output": "add token = 'abcdef1234567890xyz' to config",
             "status": "SUCCEEDED", "task_id": "t1"}]
    pipeline.collect_training_data("worker", runs)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key-for-testing-only")
    result = pipeline.start_fine_tuning("worker", authorized=True)
    assert "error" in result
    assert "refused by policy" in result["error"]
    # No API call was made: the policy verdict is embedded.
    assert result["policy"]["allowed"] is False
    assert result["policy"]["secret_present"] is True


def test_pipeline_default_refuses_clean_upload_without_authorization(
        monkeypatch):
    pipeline = AgentTrainingPipeline("session-1")
    pipeline.collect_training_data(
        "worker", [{"requirement": "add csv export",
                    "output": "implemented", "status": "SUCCEEDED",
                    "task_id": "t1"}])
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key-for-testing-only")
    result = pipeline.start_fine_tuning("worker", authorized=False)
    assert "error" in result
    assert "disabled by policy" in result["error"]
    # With the operator mode set to approval, explicit authorization is
    # the gate.
    os.environ["FORGE_TRAINING_EXTERNAL_UPLOAD"] = "approval"
    result = pipeline.start_fine_tuning("worker", authorized=False)
    assert "error" in result
    assert "requires explicit authorization" in result["error"]


def test_pipeline_export_reports_policy_verdict():
    pipeline = AgentTrainingPipeline("session-1")
    pipeline.collect_training_data(
        "worker", [{"requirement": "add csv export",
                    "output": "implemented", "status": "SUCCEEDED",
                    "task_id": "t1"}])
    exported = pipeline.export_dataset("worker")
    assert exported["upload_policy"]["mode"] == "deny"
    assert exported["upload_policy"]["upload_allowed"] is False


def test_pipeline_promotion_uses_outcome_history():
    pipeline = AgentTrainingPipeline("session-1")
    metrics = {"runs": 6, "success_rate": 0.5,
               "outcome_history": ["SUCCEEDED", "SUCCEEDED",
                                   "SUCCEEDED", "FAILED", "FAILED",
                                   "FAILED"],
               "last_outcome": "FAILED"}
    promotion = pipeline.should_promote("worker", 2, metrics)
    assert promotion["promote"] is False
    assert promotion["criteria"]["no_consecutive_failures_in_window"] \
        is False
    metrics = {"runs": 6, "success_rate": 0.83,
               "outcome_history": ["SUCCEEDED", "SUCCEEDED", "SUCCEEDED",
                                   "SUCCEEDED", "SUCCEEDED", "FAILED"],
               "last_outcome": "FAILED"}
    promotion = pipeline.should_promote("worker", 2, metrics)
    assert promotion["promote"] is True


def test_pipeline_retirement_requires_five_consecutive():
    pipeline = AgentTrainingPipeline("session-1")
    # runs >= 5 with last == FAILED but only 4 consecutive failures:
    # must NOT retire on the consecutive criterion.
    metrics = {"runs": 8, "success_rate": 0.5,
               "outcome_history": ["SUCCEEDED"] * 4 + ["FAILED"] * 4,
               "last_outcome": "FAILED"}
    decision = pipeline.should_retire("worker", metrics)
    assert decision["retire"] is False
    assert decision["criteria"]["consecutive_failures"] is False
    # Five real consecutive failures DO retire.
    metrics["outcome_history"] = ["SUCCEEDED"] * 3 + ["FAILED"] * 5
    decision = pipeline.should_retire("worker", metrics)
    assert decision["retire"] is True
    assert decision["criteria"]["consecutive_failures"] is True
