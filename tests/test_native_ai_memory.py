"""Native memory: bounded durable knowledge, credentials never stored."""
from __future__ import annotations

import json

import pytest

from forge.native.memory import (
    MemoryCategory,
    NativeMemory,
    SecretInMemoryError,
)


@pytest.fixture()
def memory(tmp_path):
    return NativeMemory(tmp_path)


def test_all_five_categories_roundtrip(memory):
    memory.record_decision("t", "decision-1")
    memory.record_strategy("t", "strategy-1", {"files_changed": 2})
    memory.record_failure("t", "assertion", "boom")
    memory.record_pattern("t", "package layout: flat modules")
    memory.record_verification("t", {"status": "PASS", "executed": 4,
                                     "passed": 4, "failed": [],
                                     "skipped": []})
    summary = memory.summary()
    assert summary["entries"] == 5
    assert set(summary["categories"]) == {c.value
                                          for c in MemoryCategory.all()}
    assert all(v == 1 for v in summary["categories"].values())


def test_recall_newest_first_and_bounded(memory):
    for i in range(8):
        memory.record_decision("task %d" % i, "decision %d" % i)
    latest = memory.decisions(limit=3)
    assert len(latest) == 3
    assert "decision 7" in json.dumps(latest[0].payload)
    assert "task 7" == latest[0].task


def test_query_terms_filter_recall(memory):
    memory.record_strategy("calc module layout", "split parser")
    memory.record_strategy("network stack tuning", "shrink buffers")
    hits = memory.successful_strategies(["calc"])
    assert len(hits) == 1
    assert "split parser" in hits[0].payload["strategy"]


def test_secret_material_is_refused_not_stored(memory, tmp_path):
    with pytest.raises(SecretInMemoryError):
        memory.record("decisions", "leak", "test",
                      {"note": 'aws_secret_access_key = "AKIA1234567890ABCDEF"'})
    files = list((tmp_path / ".forge/memory/native").rglob("*.json"))
    assert not files  # nothing hit the disk


def test_redactable_secrets_store_the_masked_form_only(memory):
    record = memory.record(
        "decisions", "t", "test",
        {"note": "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n"})
    assert record.redacted is True
    stored = memory.store.load("%s/%s.json" % (record.category, record.id))
    assert "MIIabc" not in stored
    assert "REDACTED" in stored


def test_keys_cannot_escape_the_memory_root(memory, tmp_path):
    with pytest.raises(ValueError):
        memory.store.save("../escape.json", "x")
    with pytest.raises(ValueError):
        memory.store.load("a/../../b.json")


def test_disabled_memory_writes_nothing(tmp_path):
    memory = NativeMemory(tmp_path, enabled=False)
    record = memory.record_decision("t", "volatile decision")
    assert record.id  # records are still returned (they describe the run)
    assert memory.decisions() == []
    assert not (tmp_path / ".forge/memory/native/decisions").exists() or \
        memory.store.list() == []


def test_clear_removes_entries(memory):
    memory.record_decision("t", "d1")
    memory.record_failure("t", "syntax", "s")
    assert memory.clear() == 2
    assert memory.summary()["entries"] == 0


def test_overlong_entries_are_shrunk_not_dropped(memory):
    huge = "x" * 200_000
    record = memory.record("patterns", "t", "test",
                           {"blob": huge})
    stored = memory.store.load("%s/%s.json" % (record.category, record.id))
    assert stored is not None
    parsed = json.loads(stored)
    assert len(parsed["payload"]["blob"]) < len(huge)
    assert "truncated" in parsed["payload"]["blob"]


def test_corrupt_files_are_skipped_on_recall(memory):
    memory.record_decision("t", "good")
    memory.store.save("decisions/corrupt.json", "{not json")
    recalled = memory.decisions(limit=5)
    assert [r for r in recalled] == [r for r in recalled]
    assert len(recalled) == 1
