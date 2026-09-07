"""Data classification and model data policy tests (A33)."""
import pytest

from forge.security.classification import (
    DataClassification,
    ModelDataPolicy,
    classify_text,
    rank,
)
from forge.security.policy_gate import PolicyDecision


def test_plain_project_content_is_internal_by_default():
    assert classify_text("def health(): return True\n") == DataClassification.INTERNAL
    assert classify_text("", filename="src/app.py") == DataClassification.INTERNAL


@pytest.mark.parametrize("text", [
    "api_key = 'abcdef1234567890'",
    "password: \"correct-horse-battery\"",
    "token = \"ghp_abcdef1234567890\"",
    "-----BEGIN RSA PRIVATE KEY-----\n...",
    "key = AKIAIOSFODNN7EXAMPLE",
    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz1234",
])
def test_secret_material_detected(text):
    assert classify_text(text) == DataClassification.SECRET


def test_sensitive_filenames_raise_classification():
    assert classify_text("x", filename=".env") == DataClassification.SECRET
    assert classify_text("x", filename="prod.env") == DataClassification.SECRET
    assert classify_text("x", filename="db_credentials.json") == DataClassification.CONFIDENTIAL
    assert classify_text("x", filename="notes.txt") == DataClassification.INTERNAL


def test_detection_wins_over_declared_level():
    assert classify_text("api_key = 'abcdef1234567890'",
                         declared=DataClassification.PUBLIC) == DataClassification.SECRET
    assert classify_text("plain", declared="public") == DataClassification.PUBLIC
    assert rank(DataClassification.SECRET) > rank(DataClassification.PUBLIC)


def test_model_data_policy_matrix():
    policy = ModelDataPolicy()
    # Local models always permitted.
    assert policy.evaluate(DataClassification.SECRET, local=True) == PolicyDecision.ALLOW
    # Public content flows anywhere.
    assert policy.evaluate("public", local=False) == PolicyDecision.ALLOW
    # Secret never reaches external models without explicit authorization.
    assert policy.evaluate("secret", local=False) == PolicyDecision.DENY
    assert policy.evaluate("secret", local=False, authorized=True) == PolicyDecision.ALLOW
    # Confidential is policy-controlled (default: approval).
    assert policy.evaluate("confidential", local=False) == PolicyDecision.REQUIRE_APPROVAL
    assert ModelDataPolicy(confidential_remote="deny").evaluate(
        "confidential", local=False) == PolicyDecision.DENY
    assert ModelDataPolicy(confidential_remote="allow").evaluate(
        "confidential", local=False) == PolicyDecision.ALLOW
    # Internal is configurable.
    assert policy.evaluate("internal", local=False) == PolicyDecision.ALLOW
    assert ModelDataPolicy(allow_internal_remote=False).evaluate(
        "internal", local=False) == PolicyDecision.REQUIRE_APPROVAL


def test_model_data_policy_rejects_unknown_modes():
    with pytest.raises(ValueError):
        ModelDataPolicy(confidential_remote="maybe")


def test_policy_serializes():
    assert ModelDataPolicy().to_dict() == {
        "allow_internal_remote": True, "confidential_remote": "approval"}
