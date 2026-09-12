"""File and resource locking (A81).

:class:`ResourceLockManager` gives the scheduler and any cooperating
agent a single authority for the resources two tasks must not touch at
once: repository files (shared read / exclusive write) and named shared
resources (``terminal``, ``git``, the sequential lane, ...).

Concurrency rules:

* **shared/shared** coexist; **any/exclusive** is mutually exclusive;
* writer-priority: while an exclusive waiter exists, new shared
  acquisitions are refused (no writer starvation);
* :meth:`try_acquire` grants the *whole* key set atomically, so a task
  never holds one lock while blocked on another — the scheduler uses it
  as its admission test, which makes deadlocks structurally impossible
  on the fast path;
* the blocking :meth:`acquire` is available for callers that must wait;
  it detects deadlock through the wait-for graph and raises
  :class:`DeadlockError` instead of hanging, and otherwise honors the
  timeout with :class:`LockWaitTimeout`.

All state transitions happen under one lock; the snapshot is consistent.
"""
from __future__ import annotations

import threading
import time
from typing import Any

SHARED = "shared"
EXCLUSIVE = "exclusive"


class LockError(Exception):
    """Base class for lock failures."""


class DeadlockError(LockError):
    """A wait-for cycle was detected; the wait is aborted."""


class LockWaitTimeout(LockError):
    """The blocking wait exceeded its budget without acquiring."""


def normalize_key(key: str) -> str:
    """Canonical resource key: relative posix style, no leading dots."""
    key = (key or "").strip().replace("\\", "/")
    while key.startswith("./"):
        key = key[2:]
    while key.startswith("/"):
        key = key[1:]
    return key


class _KeyState:
    __slots__ = ("shared_holders", "exclusive_holder", "shared_waiters",
                 "exclusive_waiters")

    def __init__(self) -> None:
        self.shared_holders: set[str] = set()
        self.exclusive_holder: str | None = None
        self.shared_waiters: set[str] = set()
        self.exclusive_waiters: set[str] = set()

    def granted(self) -> bool:
        return bool(self.shared_holders) or self.exclusive_holder is not None

    def holders(self) -> dict[str, str]:
        state = {}
        for holder in sorted(self.shared_holders):
            state[holder] = SHARED
        if self.exclusive_holder is not None:
            state[self.exclusive_holder] = EXCLUSIVE
        return state


class ResourceLockManager:
    """Shared/exclusive locks over canonical resource keys."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._keys: dict[str, _KeyState] = {}

    # -- admission ------------------------------------------------------------

    def _can_grant(self, state: _KeyState, mode: str) -> bool:
        if mode == SHARED:
            if state.exclusive_holder is not None:
                return False
            if state.exclusive_waiters:  # writer priority
                return False
            return True
        return not state.shared_holders and state.exclusive_holder is None

    def try_acquire(self, task_id: str, shared: tuple[str, ...] = (),
                    exclusive: tuple[str, ...] = ()
                    ) -> bool:
        """Atomically acquire every key for *task_id*, or nothing."""
        with self._condition:
            wanted = self._resolve(tuple(shared), tuple(exclusive))
            for key, mode in wanted:
                state = self._keys.setdefault(key, _KeyState())
                if not self._can_grant(state, mode):
                    return False
            for key, mode in wanted:
                state = self._keys[key]
                if mode == SHARED:
                    state.shared_holders.add(task_id)
                else:
                    state.exclusive_holder = task_id
            return True

    @staticmethod
    def _resolve(shared: tuple[str, ...],
                 exclusive: tuple[str, ...]) -> list[tuple[str, str]]:
        """(key, mode) pairs; a key requested both ways is exclusive."""
        modes: dict[str, str] = {}
        for key in shared:
            modes[normalize_key(key)] = SHARED
        for key in exclusive:
            modes[normalize_key(key)] = EXCLUSIVE
        return [(key, mode) for key, mode in sorted(modes.items())]

    def acquire(self, task_id: str, shared: tuple[str, ...] = (),
                exclusive: tuple[str, ...] = (),
                timeout: float | None = None) -> None:
        """Block until every key is granted (deadlock-safe).

        Keys are processed in a single global order, so on this path two
        tasks can never form a wait-for cycle; the detector below is the
        backstop for external callers that acquire incrementally.
        """
        deadline = None if timeout is None else time.monotonic() + max(
            0.0, timeout)
        wanted = [key for key, _ in
                  self._resolve(tuple(shared), tuple(exclusive))]
        resolved = dict(self._resolve(tuple(shared), tuple(exclusive)))
        with self._condition:
            while True:
                pending = []
                for key in wanted:
                    state = self._keys.setdefault(key, _KeyState())
                    mode = resolved[key]
                    if mode == SHARED:
                        if task_id in state.shared_holders:
                            continue
                        state.shared_waiters.add(task_id)
                    else:
                        if state.exclusive_holder == task_id:
                            continue
                        state.exclusive_waiters.add(task_id)
                    if self._can_grant(state, mode):
                        self._grant_locked(state, key, task_id, mode)
                    else:
                        pending.append(key)
                if not pending:
                    return
                if self._detect_deadlock_locked(task_id):
                    self._forget_waiters_locked(task_id)
                    raise DeadlockError(
                        f"Deadlock detected while task {task_id!r} waited "
                        f"on: {', '.join(pending)}")
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._forget_waiters_locked(task_id)
                        raise LockWaitTimeout(
                            f"Timed out waiting for: {', '.join(pending)}")
                    self._condition.wait(timeout=min(0.05, remaining))
                else:
                    self._condition.wait(timeout=0.05)

    def _grant_locked(self, state: _KeyState, key: str, task_id: str,
                      mode: str) -> None:
        del key
        if mode == SHARED:
            state.shared_holders.add(task_id)
            state.shared_waiters.discard(task_id)
        else:
            state.exclusive_holder = task_id
            state.exclusive_waiters.discard(task_id)

    def release(self, task_id: str, keys: tuple[str, ...] | None = None,
                force: bool = False) -> int:
        """Release *task_id*'s locks (all, or the given keys).

        ``force=True`` releases even when the holder has vanished from
        the book (hard-watchdog leak reclamation); returns the number of
        keys actually released.
        """
        released = 0
        with self._condition:
            wanted = (tuple(normalize_key(k) for k in keys)
                      if keys is not None else tuple(self._keys))
            scope = wanted if not force else tuple(self._keys)
            for key in scope:
                state = self._keys.get(key)
                if state is None:
                    continue
                if key in wanted:
                    if task_id in state.shared_holders or \
                            state.exclusive_holder == task_id:
                        state.shared_holders.discard(task_id)
                        if state.exclusive_holder == task_id:
                            state.exclusive_holder = None
                        released += 1
                state.shared_waiters.discard(task_id)
                state.exclusive_waiters.discard(task_id)
        if released:
            with self._condition:
                self._condition.notify_all()
        return released

    # -- deadlock detection ------------------------------------------------------

    def _detect_deadlock_locked(self, task_id: str) -> bool:
        """True when the wait-for graph has a cycle through *task_id*.

        A cycle exists when *task_id* is reachable from one of its own
        wait targets (t waits on x ... x waits on t).
        """

        def waits_on(task: str) -> set[str]:
            targets = set()
            for key, state in self._keys.items():
                if task in state.shared_waiters:
                    if state.exclusive_holder is not None:
                        targets.add(state.exclusive_holder)
                    targets.update(state.exclusive_waiters)
                if task in state.exclusive_waiters:
                    if state.exclusive_holder is not None:
                        targets.add(state.exclusive_holder)
                    targets.update(state.shared_holders)
                    targets.update(state.exclusive_waiters)
            return targets - {task}

        seen: set[str] = set()

        def visit(node: str) -> bool:
            if node == task_id:
                return True
            if node in seen:
                return False
            seen.add(node)
            for target in waits_on(node):
                if visit(target):
                    return True
            return False

        return any(visit(target) for target in waits_on(task_id))

    def detect_deadlock(self) -> list[str]:
        """Return one wait-for cycle (task ids), or ``[]`` when none.

        Conservative: reports cycles among registered waiters only.
        """
        with self._condition:
            waiters: set[str] = set()
            for state in self._keys.values():
                waiters.update(state.shared_waiters)
                waiters.update(state.exclusive_waiters)
            for waiter in sorted(waiters):
                if self._detect_deadlock_locked(waiter):
                    return [waiter]
        return []

    def _forget_waiters_locked(self, task_id: str) -> None:
        for state in self._keys.values():
            state.shared_waiters.discard(task_id)
            state.exclusive_waiters.discard(task_id)

    # -- introspection ------------------------------------------------------------

    def holders(self, key: str) -> dict[str, str]:
        with self._condition:
            state = self._keys.get(normalize_key(key))
            return {} if state is None else state.holders()

    def waiting(self) -> dict[str, list[str]]:
        """task_id -> keys it is currently waiting on."""
        with self._condition:
            result: dict[str, list[str]] = {}
            for key, state in self._keys.items():
                for waiter in state.shared_waiters | state.exclusive_waiters:
                    result.setdefault(waiter, []).append(key)
            return {task: sorted(keys) for task, keys in result.items()}

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            keys = {}
            for key in sorted(self._keys):
                state = self._keys[key]
                if not state.granted() and not state.shared_waiters \
                        and not state.exclusive_waiters:
                    continue
                keys[key] = {
                    "holders": state.holders(),
                    "shared_waiters": sorted(state.shared_waiters),
                    "exclusive_waiters": sorted(state.exclusive_waiters),
                }
            return {"keys": keys, "waiting": self.waiting()}
