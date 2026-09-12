"""A81 file/resource locking.

Shared/exclusive semantics, atomic multi-key acquisition, writer
priority, blocking acquisition with timeout, deadlock detection,
force release (leaked-lock reclamation), and snapshot introspection.
"""
from __future__ import annotations

import threading
import time

import pytest

from forge.orchestration.locking import (DeadlockError, LockWaitTimeout,
                                         ResourceLockManager)


def test_shared_locks_coexist():
    lm = ResourceLockManager()
    assert lm.try_acquire("t1", shared=("a.py",))
    assert lm.try_acquire("t2", shared=("a.py",))
    lm.release("t1")
    lm.release("t2")


def test_exclusive_excludes_everything():
    lm = ResourceLockManager()
    assert lm.try_acquire("writer", exclusive=("a.py",))
    assert not lm.try_acquire("reader", shared=("a.py",))
    assert not lm.try_acquire("writer2", exclusive=("a.py",))
    lm.release("writer")
    assert lm.try_acquire("reader", shared=("a.py",))
    assert not lm.try_acquire("writer2", exclusive=("a.py",))
    lm.release("reader")
    assert lm.try_acquire("writer2", exclusive=("a.py",))


def test_multi_key_acquire_is_atomic():
    lm = ResourceLockManager()
    assert lm.try_acquire("t1", exclusive=("a",))
    # t2 wants a (held) and b (free): must get nothing.
    assert not lm.try_acquire("t2", exclusive=("a", "b"))
    assert lm.holders("b") == {}  # b was not partially granted
    lm.release("t1")
    assert lm.try_acquire("t2", exclusive=("a", "b"))
    assert set(lm.holders("a")) == {"t2"}
    assert set(lm.holders("b")) == {"t2"}


def test_key_normalization_is_consistent():
    lm = ResourceLockManager()
    assert lm.try_acquire("t1", exclusive=("src/a.py",))
    assert not lm.try_acquire("t2", exclusive=("./src/a.py",))
    lm.release("t1")
    assert lm.try_acquire("t2", exclusive=("./src/a.py",))


def test_blocking_acquire_waits_for_release():
    lm = ResourceLockManager()
    lm.try_acquire("t1", exclusive=("res",))
    result = {}

    def waiter():
        try:
            lm.acquire("t2", exclusive=("res",), timeout=5.0)
            result["acquired"] = True
        except LockWaitTimeout:
            result["timeout"] = True

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.1)
    assert result == {}
    lm.release("t1")
    thread.join(timeout=5)
    assert result.get("acquired") is True
    lm.release("t2")


def test_blocking_acquire_timeout_raises():
    lm = ResourceLockManager()
    lm.try_acquire("t1", exclusive=("res",))
    started = time.monotonic()
    with pytest.raises(LockWaitTimeout):
        lm.acquire("t2", exclusive=("res",), timeout=0.15)
    assert time.monotonic() - started < 2.0


def test_deadlock_detection_backstop():
    """Two incrementally-acquiring callers form a wait-for cycle.

    The manager's fast path (atomic multi-key try_acquire) cannot
    deadlock; this exercises the blocking API where external callers
    acquire one key at a time, and the detector must abort the wait
    with DeadlockError instead of hanging.
    """
    lm = ResourceLockManager()
    assert lm.try_acquire("t1", exclusive=("a",))
    assert lm.try_acquire("t2", exclusive=("b",))
    errors = []

    def waiter_a():
        # t1 holds a, wants b
        try:
            lm.acquire("t1", exclusive=("b",), timeout=2.0)
            errors.append("no-deadlock-a")
        except (DeadlockError, LockWaitTimeout) as exc:
            # At least one waiter must detect the cycle; once that
            # waiter backs off, the other's cycle is broken and its
            # wait ends at the bounded timeout (never a hang).
            errors.append(f"{type(exc).__name__}-a")

    def waiter_b():
        # t2 holds b, wants a
        try:
            lm.acquire("t2", exclusive=("a",), timeout=2.0)
            errors.append("no-deadlock-b")
        except (DeadlockError, LockWaitTimeout) as exc:
            errors.append(f"{type(exc).__name__}-b")

    a = threading.Thread(target=waiter_a)
    b = threading.Thread(target=waiter_b)
    a.start()
    b.start()
    time.sleep(0.3)  # let both register as waiters
    a.join(timeout=10)
    b.join(timeout=10)
    assert not a.is_alive() and not b.is_alive()
    assert "DeadlockError-a" in errors or "DeadlockError-b" in errors


def test_no_deadlock_on_fast_path_under_contention():
    """Many tasks, overlapping keys, atomic admission: no deadlock.

    Each task needs two keys drawn from a small pool; tasks are
    admitted only when their full set is free. Everyone must finish.
    """
    lm = ResourceLockManager()
    pool = [f"res{i}" for i in range(3)]
    done = []
    lock = threading.Lock()

    def work(task_id, index):
        keys = tuple(sorted({pool[(index + i) % len(pool)]
                             for i in range(2)}))
        keys = tuple(keys) or ("resX",)
        while not lm.try_acquire(task_id, exclusive=keys):
            time.sleep(0.005)
        time.sleep(0.01)
        lm.release(task_id)
        with lock:
            done.append(task_id)

    ids = ("t1", "t2", "t3", "t4", "t5", "t6")
    threads = [threading.Thread(target=work, args=(t, i))
               for i, t in enumerate(ids)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    elapsed = time.monotonic() - started
    assert sorted(done) == ["t1", "t2", "t3", "t4", "t5", "t6"]
    assert elapsed < 15


def test_force_release_reclaims_leaked_locks():
    lm = ResourceLockManager()
    assert lm.try_acquire("ghost", exclusive=("res",))
    # "ghost" never releases (crashed task); the watchdog reclaims.
    released = lm.release("ghost", force=True)
    assert released == 1
    assert lm.try_acquire("t2", exclusive=("res",))
    lm.release("t2")


def test_writer_priority_prevents_starvation():
    lm = ResourceLockManager()
    assert lm.try_acquire("r1", shared=("res",))
    # An exclusive waiter arrives...
    got = {}

    def writer():
        lm.acquire("w", exclusive=("res",), timeout=10.0)
        got["acquired"] = True
        lm.release("w")

    thread = threading.Thread(target=writer)
    thread.start()
    time.sleep(0.1)
    # ...new shared requests must not jump ahead of it.
    assert not lm.try_acquire("r2", shared=("res",))
    lm.release("r1")
    thread.join(timeout=10)
    assert got.get("acquired") is True


def test_snapshot_reports_holders_and_waiters():
    lm = ResourceLockManager()
    lm.try_acquire("t1", shared=("a",))
    lm.try_acquire("t2", exclusive=("b",))

    def waiter():
        lm.acquire("t3", exclusive=("b",), timeout=0.5)
    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.1)
    snap = lm.snapshot()
    assert set(snap["keys"]) == {"a", "b"}
    assert snap["keys"]["b"]["holders"] == {"t2": "exclusive"}
    assert snap["keys"]["b"]["exclusive_waiters"] == ["t3"]
    assert snap["waiting"] == {"t3": ["b"]}
    lm.release("t2")
    thread.join(timeout=5)
