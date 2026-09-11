"""Long-term memory engine: persistence, retrieval, ranking, dedupe,
deletion, project isolation, flood control, summarization, correction,
provenance, and retention."""
from __future__ import annotations

import time

import pytest

from forge.control.db import Database
from forge.memory import (
    LongTermMemory,
    MemoryConfig,
    MemoryRecord,
    MemoryType,
    Retention,
)
from forge.memory.engine import MemoryNotFoundError


def make_store(tmp_path, project="demo", config=None):
    return LongTermMemory(Database(tmp_path / "memory.db"),
                          project=project, config=config)


# -- persistence -----------------------------------------------------------

def test_persistence_across_store_instances(tmp_path):
    db_path = tmp_path / "memory.db"
    first = LongTermMemory(Database(db_path), project="demo")
    record = first.remember(
        MemoryType.PROJECT, "The API is versioned under /api/v1.",
        source="coder", importance=0.8).record
    assert record is not None

    reopened = LongTermMemory(Database(db_path), project="demo")
    fetched = reopened.get(record.id)
    assert fetched is not None
    assert fetched.content == "The API is versioned under /api/v1."
    assert fetched.memory_type == "project"
    assert fetched.project == "demo"
    assert fetched.importance == pytest.approx(0.8)


def test_every_item_carries_required_fields(tmp_path):
    store = make_store(tmp_path)
    result = store.remember(
        MemoryType.DECISION, "Chose SQLite for durable memory.",
        source="planner", via="planning", confidence=0.9,
        importance=0.7, retention=Retention.PERSISTENT)
    assert result.stored
    record = result.record
    for field in ("memory_type", "project", "created_at", "source",
                  "confidence", "importance", "retention"):
        assert getattr(record, field) not in (None, "")
    assert record.memory_type == "decision"
    assert record.source == "planner"
    assert record.retention == "persistent"
    assert record.expires_at is None  # persistent never expires


# -- retrieval & ranking ----------------------------------------------------

def test_search_ranks_most_relevant_first(tmp_path):
    store = make_store(tmp_path)
    store.remember(MemoryType.PROJECT, "The build uses webpack for assets.",
                   source="coder")
    store.remember(MemoryType.PROJECT,
                   "PostgreSQL 15 powers the primary database.",
                   source="coder", importance=0.9)
    store.remember(MemoryType.PROJECT,
                   "The deployment target is a single VM.", source="coder")

    results = store.search("postgres database", k=3)
    assert results
    assert "PostgreSQL" in results[0].record.content
    # The most relevant record must outrank the unrelated ones.
    scores = [result.score for result in results]
    assert scores == sorted(scores, reverse=True)


def test_search_returns_empty_for_unrelated_query(tmp_path):
    store = make_store(tmp_path)
    store.remember(MemoryType.PROJECT, "The cache uses Redis.",
                   source="coder")
    assert store.search("completely unrelated quantum pancake") == []


def test_recall_scopes_to_requested_type(tmp_path):
    store = make_store(tmp_path)
    store.remember(MemoryType.PROJECT, "Redis cache with 5 minute TTL.",
                   source="coder")
    store.remember(MemoryType.FAILURE, "Redis cache miss stormed the DB.",
                   source="debugger")
    only_failures = store.search("cache", memory_type=MemoryType.FAILURE)
    assert only_failures
    assert all(r.record.memory_type == "failure" for r in only_failures)


# -- duplication ------------------------------------------------------------

def test_exact_duplicate_is_merged_not_stored(tmp_path):
    store = make_store(tmp_path)
    first = store.remember(
        MemoryType.PROJECT, "The cache layer uses Redis with a 5 minute TTL.",
        source="coder").record
    second = store.remember(
        MemoryType.PROJECT, "The cache layer uses Redis with a 5 minute TTL.",
        source="coder")
    assert second.status == "duplicate"
    assert second.duplicate_of == first.id
    assert second.record is None
    assert len(store.list(project="demo")) == 1


def test_near_duplicate_is_merged(tmp_path):
    store = make_store(tmp_path)
    base = ("alpha beta gamma delta epsilon zeta eta theta iota kappa")
    first = store.remember(MemoryType.PROJECT, base, source="coder").record
    second = store.remember(
        MemoryType.PROJECT, base + " extra", source="coder")
    assert second.status == "duplicate"
    assert second.duplicate_of == first.id
    assert len(store.list(project="demo")) == 1


def test_duplicates_are_project_scoped(tmp_path):
    store = make_store(tmp_path)
    first = store.remember(MemoryType.PROJECT, "Shared knowledge fact.",
                           project="alpha").record
    again = store.remember(MemoryType.PROJECT, "Shared knowledge fact.",
                           project="beta")
    assert again.stored  # same text in a different project is not a dup
    assert again.record.id != first.id


# -- deletion ---------------------------------------------------------------

def test_delete_hides_item_and_records_provenance(tmp_path):
    store = make_store(tmp_path)
    record = store.remember(MemoryType.PROJECT, "remove the legacy export module",
                            source="coder").record
    assert store.delete(record.id, source="operator", reason="stale")
    assert store.get(record.id) is None
    assert store.search("remove") == []
    assert store.list(project="demo") == []
    actions = [entry["action"] for entry in store.provenance(record.id)]
    assert "deleted" in actions


def test_delete_unknown_id_returns_false(tmp_path):
    store = make_store(tmp_path)
    assert store.delete("does-not-exist") is False


def test_purge_hard_deletes_but_keeps_provenance(tmp_path):
    store = make_store(tmp_path)
    record = store.remember(MemoryType.PROJECT, "purge the legacy export entry").record
    assert store.purge(record.id, source="operator")
    assert store.get(record.id) is None
    # The provenance trail survives the hard delete (audit only).
    assert store.provenance(record.id)


# -- project isolation ------------------------------------------------------

def test_project_isolation(tmp_path):
    store = make_store(tmp_path)
    store.remember(MemoryType.PROJECT, "alpha secret sauce",
                   project="alpha").record
    assert store.list(project="beta") == []
    assert store.search("sauce", project="beta") == []
    assert store.stats(project="beta")["total"] == 0
    # A project-bound store can never read another project's records.
    bound = LongTermMemory(Database(tmp_path / "memory.db"), project="beta")
    assert bound.list() == []


def test_get_respects_project_scope(tmp_path):
    store = make_store(tmp_path)
    record = store.remember(MemoryType.PROJECT, "alpha project uses the beta connector",
                            project="alpha").record
    assert store.get(record.id, project="beta") is None
    assert store.get(record.id, project="alpha").id == record.id


# -- flood control ----------------------------------------------------------

def test_stopword_noise_is_rejected(tmp_path):
    store = make_store(tmp_path)
    result = store.remember(MemoryType.PROJECT, "the and of to the")
    assert result.status == "rejected"
    assert store.stats()["total"] == 0


def test_too_short_content_is_rejected(tmp_path):
    store = make_store(tmp_path)
    result = store.remember(MemoryType.PROJECT, "cache")
    assert result.status == "rejected"


def test_caps_evict_least_important_oldest(tmp_path):
    store = make_store(
        tmp_path,
        config=MemoryConfig(max_entries_per_project=5,
                            max_entries_per_type=5))
    for index in range(8):
        store.remember(MemoryType.PROJECT, f"record number {index}",
                       importance=0.1 if index < 3 else 0.9)
    stats = store.stats()
    assert stats["total"] <= 5
    remaining = store.list(project="demo")
    # Low-importance early records were evicted first.
    contents = {record.content for record in remaining}
    assert "record number 0" not in contents


# -- summarization ----------------------------------------------------------

def test_summarize_compresses_items_into_one_record(tmp_path):
    store = make_store(tmp_path)
    store.remember(MemoryType.PROJECT,
                   "The cache layer uses Redis with a 5 minute TTL.")
    store.remember(MemoryType.PROJECT,
                   "The database is PostgreSQL 15 with connection pooling.")
    store.remember(MemoryType.PROJECT,
                   "The API is versioned under /api/v1.")
    summary = store.summarize(max_sentences=2)
    assert summary is not None
    assert summary.via == "summarization"
    assert summary.metadata.get("summarized_count") == 3
    assert summary.content  # non-empty extractive summary


def test_summarize_requires_multiple_items(tmp_path):
    store = make_store(tmp_path)
    store.remember(MemoryType.PROJECT, "only one item here.")
    assert store.summarize() is None


# -- correction -------------------------------------------------------------

def test_correct_supersedes_and_bumps_version(tmp_path):
    store = make_store(tmp_path)
    original = store.remember(
        MemoryType.PROJECT, "The TTL is five minutes.", source="coder",
        importance=0.5).record
    fixed = store.correct(
        original.id, "The TTL is thirty minutes.", source="human",
        reason="wrong TTL", importance=0.9)
    assert fixed.id != original.id
    assert fixed.supersedes == original.id
    assert fixed.version == original.version + 1
    assert fixed.importance == pytest.approx(0.9)

    # Only the corrected version is active; the original is superseded.
    active = store.list(project="demo")
    assert [record.id for record in active] == [fixed.id]
    stale = store.get(original.id, include_inactive=True)
    assert stale.status == "superseded"

    actions = [entry["action"] for entry in store.provenance(fixed.id)]
    assert "corrected" in actions


def test_correct_missing_id_raises(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(MemoryNotFoundError):
        store.correct("nope", "new content")


# -- retention --------------------------------------------------------------

def test_retention_expiry_filters_expired_items(tmp_path):
    store = make_store(tmp_path)
    record = store.remember(
        MemoryType.SESSION, "session scratch note", retention=Retention.EPHEMERAL,
        ttl_seconds=1).record
    assert store.get(record.id) is not None
    time.sleep(1.05)
    expired = store.enforce_retention(project="demo")
    assert expired == 1
    assert store.get(record.id) is None  # expired items are filtered out
    inactive = store.get(record.id, include_inactive=True)
    assert inactive.status == "expired"


def test_purge_expired_reclaims_rows(tmp_path):
    store = make_store(tmp_path)
    record = store.remember(
        MemoryType.SESSION, "a short-lived session note", retention=Retention.EPHEMERAL,
        ttl_seconds=1).record
    time.sleep(1.05)
    store.enforce_retention(project="demo")
    reclaimed = store.purge_expired(project="demo")
    assert reclaimed == 1
    assert store.get(record.id, include_inactive=True) is None


# -- provenance & stats -----------------------------------------------------

def test_provenance_tracks_lifecycle(tmp_path):
    store = make_store(tmp_path)
    record = store.remember(MemoryType.PROJECT, "an auditable project fact for provenance",
                            source="coder").record
    store.delete(record.id, source="operator", reason="cleanup")
    actions = [entry["action"] for entry in store.provenance(record.id)]
    assert actions == ["created", "deleted"]
    assert all(entry["actor"] for entry in store.provenance(record.id))


def test_stats_breaks_down_by_type_and_status(tmp_path):
    store = make_store(tmp_path)
    store.remember(MemoryType.PROJECT, "the project uses pytest for tests")
    store.remember(MemoryType.FAILURE, "the build failed during packaging")
    deleted = store.remember(MemoryType.TASK, "the export task completed successfully").record
    store.delete(deleted.id)
    stats = store.stats(project="demo")
    assert stats["total"] == 3
    assert stats["active"] == 2
    assert stats["deleted"] == 1
    assert stats["by_type"] == {"project": 1, "failure": 1, "task": 1}


def test_validation_rejects_unknown_type_and_retention(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.remember("not-a-type", "content")
    with pytest.raises(ValueError):
        store.remember(MemoryType.PROJECT, "content", retention="forever")
    assert store.remember(MemoryType.PROJECT, "   ").status == "rejected"
