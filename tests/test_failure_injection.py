"""Session 10 (E): failure injection.

Closes the remaining gaps in the spec's 15-category failure matrix.
Categories already covered elsewhere (and intentionally not duplicated):
worker timeout/fence + stale result (test_scheduler_fencing), lock
conflict + bounded concurrency (test_dag_scheduler), checkpoint order +
partial-apply rollback (test_a32_transaction), malformed/tampered
manifest (test_a83_ports), approval mismatch (test_a32_transaction /
test_a38_api), model/backend unavailable (test_a81_*), server restart
(test_server_restart), client reconnect (A81 reconnect + cursor replay
in test_g560_thin_client), network failure (doctor + research honesty).
"""
from __future__ import annotations

import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

from forge.agents.execution import CallableAgentExecutor
from forge.agents.registry import AgentRegistration, AgentRegistry
from forge.core.dag_scheduler import DAGScheduler, TERMINAL_STATES
from forge.core.orchestrator import MultiAgentOrchestrator


def _registry_with(name: str, worker, capability: str = "coding"):
    registry = AgentRegistry()
    registry.register(AgentRegistration(
        name, capability,
        CallableAgentExecutor(name, worker), (capability,)))
    return registry


# -- worker crash -----------------------------------------------------------------


def test_crashing_worker_fails_the_task_and_skips_dependents():
    """A worker that raises is a failure, not a hang: the task is
    FAILED with the error recorded, and the run reports FAILED."""
    registry = _registry_with(
        "crasher",
        lambda r: (_ for _ in ()).throw(RuntimeError("worker exploded")))
    orchestrator = MultiAgentOrchestrator(
        registry, max_workers=1, max_attempts=1)
    plan = orchestrator.build_plan("add a coding feature", chain=True)
    report = orchestrator.execute(plan)
    assert report.status.value == "FAILED"
    outcome = report.outcomes[0]
    assert outcome.status.value in ("FAILED", "FENCED")
    assert "worker exploded" in (outcome.error + report.summary)


def test_crash_then_success_on_retry():
    """A first attempt that crashes is retried at a higher generation;
    a healthy second attempt wins and the run succeeds."""
    calls = []

    def flaky(_request):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient crash")
        return "second attempt ok"

    registry = _registry_with("flaky", flaky)
    orchestrator = MultiAgentOrchestrator(
        registry, max_workers=1, max_attempts=2)
    plan = orchestrator.build_plan("add a coding feature", chain=True)
    report = orchestrator.execute(plan)
    assert report.status.value == "SUCCEEDED"
    assert len(calls) == 2
    outcome = report.outcomes[0]
    assert outcome.status.value == "SUCCEEDED"
    assert outcome.attempts == 2


# -- hang + cancel (no timeout configured) ------------------------------------------


def test_hung_worker_cancelled_and_late_result_rejected(tmp_path):
    """A worker that hangs forever with NO per-task timeout is fenced
    by cancel(): the task lands in a terminal state, and the result the
    worker eventually produces cannot commit."""
    store = str(tmp_path / "hang.db")
    started = threading.Event()
    release = threading.Event()

    def hang(_request):
        started.set()
        release.wait(10)
        return "too late"

    registry = _registry_with("hanger", hang)
    orchestrator = MultiAgentOrchestrator(
        registry, max_workers=1, max_attempts=1,
        task_id="hang-run", store_path=store)
    plan = orchestrator.build_plan("add a coding feature", chain=True)

    holder = {}
    thread = threading.Thread(
        target=lambda: holder.update(report=orchestrator.execute(plan)),
        daemon=True)
    thread.start()
    assert started.wait(10), "worker never started"
    time.sleep(0.05)
    orchestrator.cancel()
    thread.join(15)
    assert "report" in holder
    report = holder["report"]
    assert report.status.value in ("CANCELLED", "FAILED")
    outcome = report.outcomes[0]
    assert outcome.status.value in TERMINAL_STATES
    # The terminal state is durable.
    conn = sqlite3.connect(store)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT state, output FROM tasks").fetchone()
    conn.close()
    assert row["state"] in TERMINAL_STATES
    # The late "too late" result was never persisted.
    assert row["output"] != "too late"
    release.set()


# -- retry during shutdown -----------------------------------------------------------


def test_cancel_during_attempt_never_starts_next_attempt(tmp_path):
    """Cancelling the run while attempt 1 is in flight must not let a
    retry (attempt 2) start afterwards: the task is terminal, exactly
    one attempt was used, and the persisted record agrees."""
    store = str(tmp_path / "shutdown.db")
    started = threading.Event()
    release = threading.Event()

    def slow(_request):
        started.set()
        release.wait(5)
        return "should not happen"

    registry = _registry_with("slowpoke", slow)
    orchestrator = MultiAgentOrchestrator(
        registry, max_workers=1, max_attempts=3,
        task_id="shutdown-run", store_path=store)
    plan = orchestrator.build_plan("add a coding feature", chain=True)

    holder = {}
    thread = threading.Thread(
        target=lambda: holder.update(report=orchestrator.execute(plan)),
        daemon=True)
    thread.start()
    assert started.wait(10), "worker never started"
    time.sleep(0.05)
    orchestrator.cancel()
    thread.join(30)
    release.set()  # let the abandoned worker finish so it exits

    report = holder["report"]
    assert report.status.value in ("CANCELLED", "FAILED")
    conn = sqlite3.connect(store)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT state, attempts FROM tasks").fetchone()
    conn.close()
    assert row["state"] in TERMINAL_STATES
    # No second attempt ever started after the cancellation.
    assert row["attempts"] == 1


# -- SQLite store failure --------------------------------------------------------------


def test_unwritable_store_fails_closed(tmp_path):
    """A scheduler store that cannot be created fails loudly at
    construction — never a silent in-memory run that pretends it is
    durable."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    bad_store = str(blocker / "tasks.db")  # parent is a file

    with pytest.raises(Exception):
        DAGScheduler(bad_store)


def test_dead_store_mid_run_fails_closed_not_succeeded(tmp_path):
    """If the durable store dies mid-attempt, the attempt must fail
    with the persistence error — never a silent in-memory SUCCEEDED
    while the durable record is gone."""
    store = str(tmp_path / "io.db")
    scheduler = DAGScheduler(store, max_workers=1)
    scheduler.add_task("t1", max_attempts=1)

    def worker(task, attempt, control, guard):
        scheduler._conn.close()  # simulate the store dying now
        return {"success": True, "output": "done", "terminal": True}

    try:
        results = scheduler.run(worker)
        result = results.get("t1")
    except Exception:
        result = None
    task = scheduler.get_task("t1")
    # Honest fail-closed: a dead store can never produce a success.
    assert task.state != "SUCCEEDED"
    if result is not None:
        assert result.state == "FAILED"
        assert result.error


# -- event sequence race -----------------------------------------------------------------


def test_event_sequence_stays_monotonic_under_concurrency(tmp_path):
    """Many tasks committing in parallel against one durable store: the
    persisted event sequence is strictly monotonic, unique, gapless."""
    store = str(tmp_path / "race.db")
    scheduler = DAGScheduler(store, max_workers=8)
    for index in range(8):
        scheduler.add_task("t%d" % index, max_attempts=1)

    def worker(task, attempt, control, guard):
        time.sleep(0.005 * (int(task.task_id[1:]) % 3))
        return {"success": True, "output": "ok", "terminal": True}

    scheduler.run(worker)
    conn = sqlite3.connect(store)
    seqs = [row[0] for row in conn.execute(
        "SELECT seq FROM events ORDER BY seq")]
    conn.close()
    assert seqs, "no events persisted"
    assert seqs == list(range(1, len(seqs) + 1))  # gapless + unique
