import time
from pathlib import Path
from forge.self_development import HistoryRecord, HistoryStore


def test_history_store_save_and_query(tmp_path: Path):
    store = HistoryStore(root=tmp_path)

    rec1 = HistoryRecord(
        timestamp=time.time(),
        candidate_id="CANDIDATE-1",
        candidate_hash="hash_a",
        candidate_class="TEST_IMPROVEMENT",
        title="Add unit test",
        baseline_commit="abc123",
        checkpoint_id="ckpt-1",
        accepted=False,
    )
    store.save(rec1)

    rec2 = HistoryRecord(
        timestamp=time.time() + 1,
        candidate_id="CANDIDATE-1",
        candidate_hash="hash_a",
        candidate_class="TEST_IMPROVEMENT",
        title="Add unit test retry",
        baseline_commit="abc123",
        checkpoint_id="ckpt-2",
        accepted=False,
    )
    store.save(rec2)

    records = store.list_records()
    assert len(records) == 2

    is_rep_failed = store.is_candidate_repeatedly_failed("hash_a", max_failures=2)
    assert is_rep_failed is True
