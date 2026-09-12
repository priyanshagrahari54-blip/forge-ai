"""Task queue unit tests (A81): persistence, priority, leases, recovery.

The queue is the crash-safety spine of the server: work survives
restarts, leases make dead workers detectable, and ``recover()`` is the
single startup step that turns a crashed dispatcher state into a clean
one.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from forge.server.queue import TaskQueue  # noqa: E402
from forge.server.storage import Database  # noqa: E402


def make_queue(tmp_path, name: str = "server.db") -> TaskQueue:
    return TaskQueue(Database(tmp_path / name))


def test_enqueue_and_lease_fifo(tmp_path):
    queue = make_queue(tmp_path)
    queue.enqueue("t1", "demo")
    queue.enqueue("t2", "demo")

    assert queue.lease_next("demo", "owner-a") == "t1"
    assert queue.lease_next("demo", "owner-a") == "t2"
    assert queue.lease_next("demo", "owner-a") is None


def test_priority_beats_fifo(tmp_path):
    queue = make_queue(tmp_path)
    queue.enqueue("low", "demo", priority=1)
    queue.enqueue("high", "demo", priority=10)
    queue.enqueue("mid", "demo", priority=5)

    order = [queue.lease_next("demo", "o") for _ in range(3)]
    assert order == ["high", "mid", "low"]


def test_leased_items_are_not_double_leased(tmp_path):
    queue = make_queue(tmp_path)
    queue.enqueue("t1", "demo")
    assert queue.lease_next("demo", "owner-a") == "t1"
    # Another owner (or the same one) cannot lease it twice.
    assert queue.lease_next("demo", "owner-b") is None
    assert queue.lease_held_by("t1", "owner-a")
    assert not queue.lease_held_by("t1", "owner-b")


def test_backoff_delays_dispatch(tmp_path):
    queue = make_queue(tmp_path)
    queue.enqueue("t1", "demo", available_at=time.time() + 30.0)
    assert queue.lease_next("demo", "o") is None
    assert queue.depth() == 1
    assert queue.dispatchable_count() == 0
    # Explicit "now" past the backoff window dispatches it.
    assert queue.lease_next("demo", "o", now=time.time() + 31.0) == "t1"


def test_requeue_clears_lease_and_honors_new_backoff(tmp_path):
    queue = make_queue(tmp_path)
    queue.enqueue("t1", "demo")
    queue.lease_next("demo", "owner-a")
    queue.requeue("t1", available_at=time.time() + 30.0)
    assert not queue.lease_held_by("t1", "owner-a")
    assert queue.lease_next("demo", "owner-b") is None
    queue.requeue("t1")
    assert queue.lease_next("demo", "owner-b") == "t1"


def test_remove_drops_item(tmp_path):
    queue = make_queue(tmp_path)
    queue.enqueue("t1", "demo")
    assert queue.remove("t1")
    assert not queue.contains("t1")
    assert not queue.remove("t1")


def test_enqueue_is_idempotent_per_task(tmp_path):
    queue = make_queue(tmp_path)
    queue.enqueue("t1", "demo", priority=1)
    queue.enqueue("t1", "demo", priority=7)  # refresh, not duplicate
    assert queue.depth() == 1
    rows = queue.peek("demo")
    assert [row["task_id"] for row in rows] == ["t1"]
    assert rows[0]["priority"] == 7  # refreshed priority wins
    assert queue.lease_next("demo", "o") == "t1"
    leased = queue.peek("demo")
    assert leased[0]["lease_owner"] == "o"


def test_queue_persists_across_reopen(tmp_path):
    queue = make_queue(tmp_path)
    queue.enqueue("t1", "demo", priority=3)
    queue.enqueue("t2", "demo")

    reopened = make_queue(tmp_path)
    assert reopened.depth() == 2
    assert reopened.lease_next("demo", "owner") == "t1"


def test_recover_clears_stale_leases(tmp_path):
    queue = make_queue(tmp_path)
    queue.enqueue("t1", "demo")
    queue.enqueue("t2", "demo")
    queue.lease_next("demo", "dead-worker-1")
    queue.lease_next("demo", "dead-worker-2")
    assert queue.leased_count() == 2

    # Simulated restart: a fresh queue over the same database recovers.
    restarted = make_queue(tmp_path)
    assert restarted.recover() == 2
    assert restarted.leased_count() == 0
    assert not restarted.lease_held_by("t1", "dead-worker-1")
    # Work is dispatchable again after recovery.
    assert restarted.lease_next("demo", "live-worker") == "t1"


def test_position_is_priority_aware(tmp_path):
    queue = make_queue(tmp_path)
    queue.enqueue("low", "demo", priority=1)
    queue.enqueue("high", "demo", priority=9)
    assert queue.position("high") == 1
    assert queue.position("low") == 2
    assert queue.position("absent") == 0


def test_projects_are_isolated(tmp_path):
    queue = make_queue(tmp_path)
    queue.enqueue("a1", "alpha")
    queue.enqueue("b1", "beta")
    assert queue.lease_next("alpha", "o") == "a1"
    assert queue.lease_next("alpha", "o") is None
    assert queue.lease_next("beta", "o") == "b1"
    assert queue.depth("alpha") == 1  # still leased, still counted
    assert queue.depth() == 2
