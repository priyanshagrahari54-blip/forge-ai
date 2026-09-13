"""Secret detection and redaction for long-term memory.

Forge memory must never persist API keys, passwords, tokens, private keys,
or secrets. These tests assert that every candidate is scanned before
storage, secret spans are redacted in place, and pure-secret content is
refused outright.

Secret-looking samples are assembled at runtime (never written as literal
key strings in the source) so they exercise the scanner without tripping
repository secret-scanning tooling.
"""
from __future__ import annotations

import pytest

from forge.control.db import Database
from forge.memory import LongTermMemory, MemoryType
from forge.memory.redaction import (
    REDACTED,
    contains_secret,
    redact,
    scan_secrets,
)


def _join(*parts):
    return "".join(parts)


def _fake_secret_samples():
    return [
        _join("sk-", "a" * 32),
        _join("github_pat_", "A" * 20, "B" * 6),
        _join("ghp_", "A" * 30),
        _join("AIzaSyD-", "a" * 35),
        _join("xoxb-", "1" * 12, "-", "2" * 12, "-", "a" * 16),
        _join("AKIA", "IOSFODNN7EXAMPLE"),
        _join("eyJ", "a" * 10, ".", "b" * 12, ".", "c" * 14),
    ]


SK_SAMPLE = _join("sk-", "a" * 32)
AKIA_SAMPLE = _join("AKIA", "IOSFODNN7EXAMPLE")


@pytest.mark.parametrize("secret", _fake_secret_samples())
def test_secret_spans_are_detected_and_redacted(secret):
    scan = scan_secrets(secret)
    assert not scan.clean
    assert contains_secret(secret)


def test_assignment_style_secrets_are_redacted():
    content = "set the password=hunter2 and api_key='abcd1234xyz' in config"
    redacted = redact(content)
    assert "hunter2" not in redacted
    assert "abcd1234xyz" not in redacted
    assert REDACTED in redacted


def test_private_key_block_is_redacted():
    content = (
        "the deploy key is\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAw0...\n"
        "-----END RSA PRIVATE KEY-----\n"
        "and must be kept safe"
    )
    redacted = redact(content)
    assert "PRIVATE KEY" not in redacted
    assert REDACTED in redacted


def test_engine_never_persists_secret_spans(tmp_path):
    store = LongTermMemory(Database(tmp_path / "memory.db"), project="demo")
    result = store.remember(
        MemoryType.PROJECT,
        "The payment provider API key " + SK_SAMPLE
        + " lives in the environment.",
        source="coder")
    assert result.stored
    assert SK_SAMPLE not in result.record.content
    assert result.record.redaction_count >= 1
    assert REDACTED in result.record.content


def test_engine_refuses_pure_secret_content(tmp_path):
    store = LongTermMemory(Database(tmp_path / "memory.db"), project="demo")
    result = store.remember(MemoryType.PROJECT, SK_SAMPLE)
    assert result.status == "rejected"
    assert store.stats()["total"] == 0


def test_redaction_count_is_reported(tmp_path):
    store = LongTermMemory(Database(tmp_path / "memory.db"), project="demo")
    result = store.remember(
        MemoryType.PROJECT,
        "aws=" + AKIA_SAMPLE + " and token=abcd1234efgh5678",
        source="coder")
    assert result.stored
    assert result.record.redaction_count >= 2
    assert AKIA_SAMPLE not in result.record.content


def test_clean_content_passes_through_unchanged():
    text = "The cache layer uses Redis with a 5 minute TTL."
    scan = scan_secrets(text)
    assert scan.clean
    assert scan.redacted_text == text
    assert not contains_secret(text)
