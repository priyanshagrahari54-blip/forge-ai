"""Session memory store (A37): bounded, session-scoped, durable."""
from __future__ import annotations

import pytest

from forge.control.db import Database
from forge.control.memory import (MAX_CONTENT_BYTES, MemoryEntry,
                                  SessionMemoryStore)


def make_store(tmp_path):
    return SessionMemoryStore(Database(tmp_path / "memory.db"))


def test_add_list_get_delete_round_trip(tmp_path):
    store = make_store(tmp_path)
    entry = store.add("s1", "note", "remember the API design",
                      source="alice")
    assert isinstance(entry, MemoryEntry)
    listed = store.list("s1")
    assert [item.id for item in listed] == [entry.id]
    assert store.get("s1", entry.id).content == "remember the API design"
    assert store.get("s2", entry.id) is None  # session-scoped
    assert store.delete("s2", entry.id) is False
    assert store.delete("s1", entry.id) is True
    assert store.list("s1") == []


def test_entries_are_session_scoped(tmp_path):
    store = make_store(tmp_path)
    alice = store.add("s1", "fact", "alice fact")
    bob = store.add("s2", "fact", "bob fact")
    assert [e.id for e in store.list("s1")] == [alice.id]
    assert [e.id for e in store.list("s2")] == [bob.id]


def test_kind_and_content_validation(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.add("s1", "password", "x")  # not a valid kind
    with pytest.raises(ValueError):
        store.add("s1", "note", "")
    with pytest.raises(ValueError):
        store.add("s1", "note", "x" * (MAX_CONTENT_BYTES + 1))
    with pytest.raises(ValueError):
        store.add("", "note", "x")


def test_size_pruning_keeps_newest(tmp_path):
    store = make_store(tmp_path)
    for index in range(40):
        store.add("s1", "fact", "x" * 9000, source=f"src-{index}")
    listed = store.list("s1")
    # 40 * ~9 KB = ~360 KB < 1 MB cap: nothing pruned by bytes.
    assert len(listed) == 40
    # Cross the total-byte cap; oldest entries are pruned first.
    for index in range(100):
        store.add("s1", "fact", "y" * 9000, source=f"more-{index}")
    remaining = store.list("s1")
    assert store.total_bytes("s1") <= 1_000_000
    assert remaining[0].source.startswith("more-")  # newest survives


def test_entry_count_cap(tmp_path):
    store = make_store(tmp_path)
    for index in range(520):
        store.add("s1", "note", f"entry {index}")
    listed = store.list("s1")
    assert len(listed) == 500
    assert listed[0].content == "entry 519"  # newest first


def test_persistence_across_store_instances(tmp_path):
    db_path = tmp_path / "memory.db"
    store = SessionMemoryStore(Database(db_path))
    entry = store.add("s1", "summary", "the run passed")
    reopened = SessionMemoryStore(Database(db_path))
    assert reopened.get("s1", entry.id).content == "the run passed"
