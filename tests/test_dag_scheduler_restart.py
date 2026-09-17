from __future__ import annotations

from forge.core.dag_scheduler import DAGScheduler


def test_running_persisted_task_is_requeued_with_remaining_attempt(tmp_path):
    store = str(tmp_path / "scheduler.db")
    first = DAGScheduler(store_path=store, max_workers=1)
    first.add_task("step-1", "resume me", max_attempts=2)
    with first._db_lock:
        first._conn.execute(
            "UPDATE tasks SET state='RUNNING', attempts=1, error='old worker' "
            "WHERE task_id='step-1'"
        )
        first._conn.commit()
    first._close_store()

    resumed = DAGScheduler(store_path=store, max_workers=1)
    adopted = resumed.add_task("step-1", "resume me", max_attempts=2)
    assert adopted.state == "QUEUED"
    assert adopted.attempts_used == 1
    assert "re-queued" in adopted.error

    results = resumed.run(
        lambda task, attempt, control, guard: {
            "success": True, "output": "recovered", "terminal": True
        }
    )
    assert results["step-1"].state == "SUCCEEDED"
    assert results["step-1"].attempts == 2
    assert results["step-1"].output == "recovered"
    resumed._close_store()
