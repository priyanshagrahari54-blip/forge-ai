"""Performance benchmark and correctness tests for RelevanceRanker."""
from __future__ import annotations

import time
from forge.memory.types import MemoryRecord, ACTIVE, Retention, MemoryType
from forge.memory.relevance import RelevanceRanker


def test_relevance_ranker_correctness_and_edge_cases():
    records = [
        MemoryRecord(
            id="m-1",
            memory_type=MemoryType.DECISION,
            project="demo",
            content="Use PostgreSQL for durable storage and fast query performance",
            summary="Database selection decision",
            source="test",
            via="test",
            confidence=0.9,
            importance=0.8,
            retention=Retention.PROJECT,
            expires_at=None,
            created_at=1700000000.0,
            updated_at=1700000000.0,
            last_accessed_at=1700000000.0,
            access_count=1,
            fingerprint="fp-1",
            status=ACTIVE,
            version=1,
            supersedes="",
            metadata={},
            redaction_count=0,
        ),
        MemoryRecord(
            id="m-2",
            memory_type=MemoryType.TASK,
            project="demo",
            content="Optimize memory retrieval ranking algorithm for speed",
            summary="Performance task",
            source="test",
            via="test",
            confidence=0.95,
            importance=0.9,
            retention=Retention.TASK,
            expires_at=None,
            created_at=1700000001.0,
            updated_at=1700000001.0,
            last_accessed_at=1700000001.0,
            access_count=1,
            fingerprint="fp-2",
            status=ACTIVE,
            version=1,
            supersedes="",
            metadata={},
            redaction_count=0,
        ),
    ]

    ranker = RelevanceRanker(records, now=1700000010.0)

    # Empty query returns empty list
    assert ranker.rank("") == []
    assert ranker.rank("a an the") == []  # Only stopwords

    # Query matching item
    results = ranker.rank("PostgreSQL storage")
    assert len(results) == 1
    assert results[0].record.id == "m-1"
    assert "postgresql" in results[0].matched_terms or "storage" in results[0].matched_terms

    # Check _lexical fallback compatibility
    lexical_score = ranker._lexical(["postgresql", "storage"], ["postgresql", "storage", "database"])
    assert lexical_score > 0.0


def test_relevance_ranker_performance():
    # Construct 500 candidate memory records
    records = [
        MemoryRecord(
            id=f"m-{i}",
            memory_type=MemoryType.DECISION if i % 2 == 0 else MemoryType.TASK,
            project="demo",
            content=f"Memory record {i} discussing caching routing database queries performance optimization {i % 10}",
            summary=f"Summary for item {i} regarding performance {i % 5}",
            source="test",
            via="test",
            confidence=0.9,
            importance=0.8,
            retention=Retention.PROJECT,
            expires_at=None,
            created_at=1700000000.0 + i,
            updated_at=1700000000.0 + i,
            last_accessed_at=1700000000.0 + i,
            access_count=1,
            fingerprint=f"fp-{i}",
            status=ACTIVE,
            version=1,
            supersedes="",
            metadata={},
            redaction_count=0,
        )
        for i in range(500)
    ]

    ranker = RelevanceRanker(records, now=1700001000.0)

    t0 = time.perf_counter()
    for _ in range(50):
        res = ranker.rank("caching routing performance optimization")
        assert len(res) > 0
    t1 = time.perf_counter()

    elapsed = t1 - t0
    # 50 rank operations over 500 records should complete rapidly (well under 1 second).
    assert elapsed < 1.0, f"RelevanceRanker.rank took {elapsed:.3f}s, expected < 1.0s"
