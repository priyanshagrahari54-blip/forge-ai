"""Restart recovery regressions for A38 orchestration and DAG state."""
from __future__ import annotations

import threading

from forge.control.control_plane import ControlConfig, ControlPlane
from forge.core.dag_scheduler import DAGScheduler, DAGSchedulerError
from helpers_a34 import ScriptedProvider, make_fabric


def test_dag_adopts_terminal_persisted_task_without_overwriting(tmp_path):
    store = str(tmp_path / "scheduler.db")

    scheduler = DAGScheduler(store_path=store, max_workers=1)
    scheduler.add_task("step-1", "persisted instruction", max_attempts=2)
    results = scheduler.run(
        lambda task, attempt, control, guard: {
            "success": True, "output": "finished", "terminal": True
        }
    )
    assert results["step-1"].state == "SUCCEEDED"
    scheduler._close_store()

    resumed = DAGScheduler(store_path=store, max_workers=1)
    adopted = resumed.add_task(
        "step-1", "persisted instruction", max_attempts=2
    )
    assert adopted.state == "SUCCEEDED"
    assert adopted.attempts_used == 1

    calls = []
    final = resumed.run(
        lambda task, attempt, control, guard: calls.append(task.task_id) or {
            "success": True, "output": "must-not-run", "terminal": True
        }
    )
    assert calls == []
    assert final["step-1"].state == "SUCCEEDED"
    assert final["step-1"].output == "finished"
    resumed._close_store()


def test_dag_rejects_persisted_definition_mismatch(tmp_path):
    store = str(tmp_path / "scheduler.db")
    first = DAGScheduler(store_path=store)
    first.add_task("step-1", "original", max_attempts=2)
    first.run(lambda task, attempt, control, guard: {
        "success": True, "output": "ok", "terminal": True
    })
    first._close_store()

    second = DAGScheduler(store_path=store)
    try:
        try:
            second.add_task("step-1", "changed", max_attempts=2)
        except DAGSchedulerError:
            pass
        else:
            raise AssertionError("expected persisted definition mismatch")
    finally:
        second._close_store()


def test_active_orchestration_is_requeued_on_plane_restart(tmp_path):
    root = tmp_path / "demo"
    root.mkdir()
    db_path = tmp_path / "cockpit.db"
    config = ControlConfig(
        db_path=str(db_path),
        projects={"demo": str(root)},
        fabric=make_fabric(ScriptedProvider()),
        max_workers=1,
    )

    plane = ControlPlane(config)
    session, _token = plane.create_session("alice", "demo")
    record = plane.submit_orchestration(
        session, "restart me", chain=True
    )
    assert record.plan().get("chain") is True
    plane.orchestrations.mutate(
        record.id, status="RUNNING", stage="running"
    )
    plane.close()

    resumed = ControlPlane(config)
    calls = []
    started = threading.Event()

    def fake_execute(orchestration_id, chain=False):
        calls.append((orchestration_id, chain))
        started.set()

    resumed._execute_orchestration = fake_execute
    resumed.start()
    try:
        assert started.wait(timeout=5), "orchestration was not redispatched"
        assert calls == [(record.id, True)]
        recovered = resumed.orchestrations.get(record.id)
        assert recovered is not None
        assert recovered.status.value == "QUEUED"
    finally:
        resumed.close()
