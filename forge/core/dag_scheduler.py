"""Dependency-aware DAG scheduler with execution fencing (Session 10).

The canonical in-process scheduler for dependency-ordered task graphs
(A38's multi-agent plans run on top of it). Its defining property is the
one the old thread-waiting design could not provide:

**A timed-out or cancelled worker cannot modify shared Forge state after
the scheduler has marked the attempt failed.**

Every attempt has a unique identity — ``(task_id, generation)`` — owned
by a :class:`forge.core.fencing.FenceRegistry`. The scheduler:

* transitions attempts through an explicit state machine
  (``QUEUED → RUNNING → SUCCEEDED | FAILED | CANCELLING → CANCELLED |
  TIMED_OUT → FENCED``) with deterministic terminal states;
* fences an attempt the moment its watchdog expires or the operator
  cancels it — from then on the attempt's results are rejected
  (:class:`~forge.core.fencing.StaleAttemptError`) and its
  ``commit_guard`` refuses every further write;
* never overlaps a retry with an abandoned previous attempt: a new
  generation starts only after the abandoned thread has either finished
  or been declared abandoned (its writes stay fenced either way);
* persists tasks, attempts, and an append-only, monotonically sequenced
  event log to SQLite when a store path is configured;
* on restart, fences orphaned ``RUNNING`` attempts (their worker is
  dead in the old process) and re-queues retryable work — a terminal
  task is never resurrected.

Concurrency is bounded by a fixed worker pool. Resource locks are named
and exclusive: a task that requests a lock held by a running task fails
deterministically with ``LOCK_CONFLICT`` instead of waiting (no
deadlock, no hidden queue).

The executor contract::

    executor(task, attempt, control, guard) -> dict

``task`` is the :class:`ScheduledTask`, ``attempt`` the
:class:`~forge.core.fencing.AttemptFence` identity, ``control`` a
:class:`~forge.core.run_control.SupervisorControl` the executor should
poll at stage boundaries (``control.checkpoint()`` raises
``TaskCancelled`` once cancelled), and ``guard`` the commit-guard
callable — every state-changing operation the executor performs must
first check ``guard()`` and refuse with the returned reason when it is
non-empty.

Return value: ``{"success": bool, "output": str, "error": str,
"cancelled": bool, "terminal": bool, "metadata": dict}``. A result from
a fenced attempt is discarded; the scheduler records why.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from forge.core.fencing import (
    CANCELLED,
    CANCELLING,
    FAILED,
    FENCED,
    QUEUED,
    RUNNING,
    SKIPPED,
    SUCCEEDED,
    AttemptFence,
    FenceRegistry,
    StaleAttemptError,
)
from forge.core.run_control import SupervisorControl, TaskCancelled

__all__ = [
    "DAGScheduler",
    "DAGSchedulerError",
    "LockConflictError",
    "ScheduledTask",
    "TaskResult",
    "TERMINAL_STATES",
]

TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED, FENCED, SKIPPED})

#: How long the scheduler waits for an abandoned (fenced) worker thread
#: to finish before declaring it abandoned and ending the task. The
#: thread stays fenced the whole time; it is only a question of whether
#: the retry may start.
DEFAULT_ABANDON_WAIT = 5.0

#: Grace between ``CANCELLING`` and the confirmed ``CANCELLED``: the
#: worker confirms at its next checkpoint; silence this long means the
#: attempt is fenced and the task recorded CANCELLED anyway.
DEFAULT_CANCEL_GRACE = 5.0

#: Bounded detail size persisted in the event log.
MAX_EVENT_DETAIL = 2000
MAX_TASK_ERROR = 2000


class DAGSchedulerError(ValueError):
    """Malformed graph or unsupported operation."""


class LockConflictError(DAGSchedulerError):
    """A requested resource lock is held by another running task."""


@dataclass
class ScheduledTask:
    """One node of the DAG plus its live, scheduler-owned state."""

    task_id: str
    description: str = ""
    depends_on: tuple = ()
    timeout: Optional[float] = None
    max_attempts: int = 1
    resources: tuple = ()
    priority: int = 0
    metadata: dict = field(default_factory=dict)
    #: Live state (not persisted as such; persistence mirrors it).
    state: str = QUEUED
    attempts_used: int = 0
    error: str = ""
    output: str = ""
    result_metadata: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass
class TaskResult:
    """Terminal outcome of one task."""

    task_id: str
    state: str
    attempts: int = 0
    output: str = ""
    error: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.state == SUCCEEDED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "state": self.state,
            "attempts": self.attempts,
            "output": self.output[:4000],
            "error": self.error[:MAX_TASK_ERROR],
            "metadata": dict(self.metadata),
        }


ExecutorFn = Callable[[ScheduledTask, AttemptFence, SupervisorControl,
                       Callable[[], str]], Dict[str, Any]]


class DAGScheduler:
    """Execute a dependency-ordered task graph with bounded concurrency.

    ``store_path`` enables durable persistence (SQLite) and restart
    recovery; without it the scheduler is in-memory only. ``on_event``
    receives ``(task_id, event_dict)`` for every state change and is
    never allowed to break the scheduler.
    """

    def __init__(self, store_path: str = "", *, max_workers: int = 4,
                 on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 owner: str = "",
                 abandon_wait: float = DEFAULT_ABANDON_WAIT,
                 cancel_grace: float = DEFAULT_CANCEL_GRACE) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if abandon_wait <= 0 or cancel_grace <= 0:
            raise ValueError("abandon_wait and cancel_grace must be positive")
        self.max_workers = int(max_workers)
        self.on_event = on_event
        self.owner = owner
        self.abandon_wait = float(abandon_wait)
        self.cancel_grace = float(cancel_grace)
        self.registry = FenceRegistry()
        self._tasks: Dict[str, ScheduledTask] = {}
        self._store_path = str(store_path or "")
        self._conn: Optional[sqlite3.Connection] = None
        self._db_lock = threading.Lock()
        #: Guards task-state transitions against external cancel() calls
        #: racing the dispatch loop. Re-entrant: the run-loop thread may
        #: enter nested transition helpers.
        self._state_lock = threading.RLock()
        self._lock_holders: Dict[str, str] = {}
        self._cancel_requested: Set[str] = set()
        self._run_cancelled = False
        self._controls: Dict[str, SupervisorControl] = {}
        if self._store_path:
            self._open_store()

    # -- persistence -------------------------------------------------------

    def _open_store(self) -> None:
        path = Path(self._store_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False is safe here: every statement runs
        # under self._db_lock, so the connection is effectively
        # single-writer even though worker threads touch it.
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._db_lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    dependencies TEXT NOT NULL,
                    timeout REAL,
                    max_attempts INTEGER NOT NULL,
                    resources TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    error TEXT NOT NULL,
                    output TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    task_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    output TEXT NOT NULL,
                    error TEXT NOT NULL,
                    PRIMARY KEY (task_id, generation)
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    ts REAL NOT NULL
                );
                """
            )
            self._conn.commit()
            # Restart recovery for attempt identity: a fresh process must
            # continue the generation high-water mark persisted by its
            # predecessor, or a stale attempt from the old process could
            # masquerade as the current one.
            for row in self._conn.execute(
                    "SELECT task_id, MAX(generation) AS g FROM attempts"
                    " GROUP BY task_id"):
                self.registry.seed_generation(row["task_id"],
                                              int(row["g"] or 0))

    def _close_store(self) -> None:
        with self._db_lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def _persist_task(self, task: ScheduledTask) -> None:
        if self._conn is None:
            return
        with self._db_lock:
            self._conn.execute(
                """
                INSERT INTO tasks (task_id, description, dependencies,
                    timeout, max_attempts, resources, priority, state,
                    attempts, error, output, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    state = excluded.state,
                    attempts = excluded.attempts,
                    error = excluded.error,
                    output = excluded.output,
                    updated_at = excluded.updated_at
                """,
                (task.task_id, task.description,
                 json.dumps(list(task.depends_on)),
                 task.timeout, task.max_attempts,
                 json.dumps(list(task.resources)), task.priority,
                 task.state, task.attempts_used,
                 task.error[:MAX_TASK_ERROR], task.output[:4000],
                 task.created_at, time.time()))
            self._conn.commit()

    def _persist_attempt(self, task_id: str, generation: int, state: str,
                         reason: str, started_at: float,
                         finished_at: float, output: str,
                         error: str) -> None:
        if self._conn is None:
            return
        with self._db_lock:
            self._conn.execute(
                """
                INSERT INTO attempts (task_id, generation, state, reason,
                    started_at, finished_at, output, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, generation) DO UPDATE SET
                    state = excluded.state,
                    reason = excluded.reason,
                    finished_at = excluded.finished_at,
                    output = excluded.output,
                    error = excluded.error
                """,
                (task_id, generation, state, reason, started_at,
                 finished_at, output[:4000], error[:MAX_TASK_ERROR]))
            self._conn.commit()

    def _record_event(self, task_id: str, attempt_id: str, generation: int,
                      state: str, detail: str) -> int:
        """Append one durable, monotonically sequenced event.

        Returns the sequence number (durable across restarts when the
        store is configured; an in-memory counter otherwise).
        """
        detail = (detail or "")[:MAX_EVENT_DETAIL]
        ts = time.time()
        if self._conn is not None:
            with self._db_lock:
                cursor = self._conn.execute(
                    "INSERT INTO events (task_id, attempt_id, generation,"
                    " state, detail, ts) VALUES (?, ?, ?, ?, ?, ?)",
                    (task_id, attempt_id, generation, state, detail, ts))
                self._conn.commit()
                return int(cursor.lastrowid or 0)
        with self._db_lock:
            self._mem_seq = getattr(self, "_mem_seq", 0) + 1
            seq = self._mem_seq
        self._emit(task_id, {
            "event": "state", "state": state, "attempt_id": attempt_id,
            "generation": generation, "detail": detail, "seq": seq,
            "ts": ts})
        return seq

    def _emit(self, task_id: str, event: Dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(task_id, event)
        except Exception:
            # Observability must never break scheduling or enforcement.
            pass

    # -- graph ---------------------------------------------------------------

    def add_task(self, task_id: str, description: str = "", *,
                 depends_on: Sequence[str] = (), timeout: Optional[float] = None,
                 max_attempts: int = 1, resources: Sequence[str] = (),
                 priority: int = 0, metadata: Optional[dict] = None) -> ScheduledTask:
        task_id = str(task_id or "").strip()
        if not task_id:
            raise DAGSchedulerError("task_id must be non-empty")
        if task_id in self._tasks:
            raise DAGSchedulerError("task already exists: %s" % task_id)
        deps = tuple(str(d) for d in (depends_on or ()))
        if any(d == task_id for d in deps):
            raise DAGSchedulerError(
                "task %s depends on itself" % task_id)
        for dep in deps:
            if dep not in self._tasks:
                raise DAGSchedulerError(
                    "task %s depends on unknown task %s" % (task_id, dep))
        if max_attempts < 1:
            raise DAGSchedulerError("max_attempts must be >= 1")
        if timeout is not None and timeout <= 0:
            raise DAGSchedulerError("timeout must be positive")
        task = ScheduledTask(
            task_id=task_id, description=str(description or ""),
            depends_on=deps, timeout=timeout,
            max_attempts=int(max_attempts),
            resources=tuple(str(r) for r in (resources or ())),
            priority=int(priority),
            metadata=dict(metadata or {}))
        self._tasks[task_id] = task
        self._detect_cycle()
        self._persist_task(task)
        self._record_event(task_id, "-", 0, QUEUED, "task added")
        return task

    def _detect_cycle(self) -> None:
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {t: WHITE for t in self._tasks}

        def visit(node: str) -> None:
            color[node] = GRAY
            for dep in self._tasks[node].depends_on:
                if color.get(dep, BLACK) == GRAY:
                    raise DAGSchedulerError(
                        "dependency cycle involving %s" % node)
                if color.get(dep, BLACK) == WHITE:
                    visit(dep)
            color[node] = BLACK

        for node in self._tasks:
            if color[node] == WHITE:
                visit(node)

    def tasks(self) -> List[ScheduledTask]:
        return sorted(self._tasks.values(), key=lambda t: t.task_id)

    def get_task(self, task_id: str) -> Optional[ScheduledTask]:
        return self._tasks.get(task_id)

    def events(self, task_id: str = "", limit: int = 50) -> List[dict]:
        """Most recent events first (durable when the store is set)."""
        limit = max(0, int(limit))
        if limit <= 0:
            return []
        if self._conn is None:
            return []
        with self._db_lock:
            if task_id:
                rows = self._conn.execute(
                    "SELECT seq, task_id, attempt_id, generation, state,"
                    " detail, ts FROM events WHERE task_id = ?"
                    " ORDER BY seq DESC LIMIT ?", (task_id, limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT seq, task_id, attempt_id, generation, state,"
                    " detail, ts FROM events ORDER BY seq DESC LIMIT ?",
                    (limit,)).fetchall()
        return [dict(row) for row in rows]

    # -- cancellation ---------------------------------------------------------

    def cancel(self, task_id: str) -> bool:
        """Request cancellation of one task. True when the request landed
        on a live (queued or running) attempt."""
        with self._state_lock:
            task = self._tasks.get(task_id)
            if task is None or task.state in TERMINAL_STATES:
                return False
            if task.state == QUEUED:
                self._finalize(task, CANCELLED,
                               "cancelled by operator before start")
                return True
            self._cancel_requested.add(task_id)
            fence = self.registry.current(task_id)
            if fence is not None and fence.state == RUNNING:
                try:
                    self.registry.cancel(task_id)
                except StaleAttemptError:
                    return True
                self._record_event(
                    task_id, fence.attempt_id, fence.generation, CANCELLING,
                    "cancel requested")
                control = self._controls.get(task_id)
                if control is not None:
                    control.request_cancel()
                task.state = CANCELLING
                self._persist_task(task)
                return True
            return task.state == CANCELLING

    def cancel_all(self) -> int:
        count = 0
        for task in self._tasks.values():
            if task.state in TERMINAL_STATES:
                continue
            if self.cancel(task.task_id):
                count += 1
        self._run_cancelled = True
        return count

    # -- execution ---------------------------------------------------------------

    def run(self, executor: ExecutorFn,
            default_timeout: Optional[float] = None) -> Dict[str, TaskResult]:
        """Run the graph to completion. Deterministic terminal state for
        every task. Never raises for executor failures (they become task
        state); raises only for a scheduler-level programming error.

        Bounded executors are always joined before this returns. A
        genuinely non-terminating executor thread cannot be killed in
        pure Python; it is fenced (its writes and its commit are refused)
        and outlives the call — documented, never silently trusted.
        """
        self._recover()
        pool = ThreadPoolExecutor(
            max_workers=self.max_workers, thread_name_prefix="forge-dag")
        running: Dict[str, _RunningAttempt] = {}
        self._controls = {}
        try:
            while True:
                self._sweep(running)
                if self._dispatch(running, pool, executor,
                                  default_timeout):
                    continue
                if not running and self._all_terminal():
                    break
                if not running:
                    # Nothing dispatched and not all terminal: the rest
                    # cannot run (failed dependencies or locked). Make it
                    # deterministic now instead of spinning.
                    self._resolve_unschedulable()
                    if self._all_terminal():
                        break
                    if not self._dispatch(running, pool, executor,
                                          default_timeout):
                        self._resolve_unschedulable()
                        if self._all_terminal():
                            break
                        # Still not terminal and nothing to do: cannot
                        # happen on a validated graph; fail closed.
                        for task in self._tasks.values():
                            if task.state not in TERMINAL_STATES:
                                self._finalize(task, FAILED,
                                               "scheduling deadlock "
                                               "(scheduler bug guard)")
                        break
                # Wait for progress: either an attempt finishes or a
                # deadline expires (bounded poll keeps the watchdog
                # responsive without burning the CPU).
                self._wait(running)
        finally:
            for attempt in list(running.values()):
                future = attempt.future
                if not future.done():
                    # The pool shutdown waits for in-flight work; fenced
                    # attempts keep refusing writes until the thread
                    # actually ends, so no late state ever lands.
                    future.cancel()
            pool.shutdown(wait=True)
            self._controls = {}
        return {task.task_id: TaskResult(
            task_id=task.task_id, state=task.state,
            attempts=task.attempts_used, output=task.output,
            error=task.error, metadata=dict(task.result_metadata))
            for task in self._tasks.values()}

    # -- internals ----------------------------------------------------------------

    @dataclass
    class _RunningAttempt:
        task_id: str
        fence: AttemptFence
        control: SupervisorControl
        guard: Callable[[], str]
        future: Future
        deadline: Optional[float]
        dispatched_at: float
        abandon_at: Optional[float] = None
        cancel_at: Optional[float] = None

    def _recover(self) -> None:
        """Fence orphaned work from a previous process; re-queue retryable
        tasks; never touch a terminal state."""
        if self._conn is None:
            return
        with self._db_lock:
            rows = self._conn.execute(
                "SELECT task_id, description, dependencies, timeout,"
                " max_attempts, resources, state, attempts, error"
                " FROM tasks"
            ).fetchall()
        for row in rows:
            task_id = row["task_id"]
            state = row["state"]
            attempts = int(row["attempts"] or 0)
            max_attempts = int(row["max_attempts"] or 1)
            if state in TERMINAL_STATES:
                # Adopt read-only so a new add_task with the same id is
                # refused instead of silently overwriting a terminal
                # row; the state itself is never touched.
                self._adopt_persisted(row, state)
                continue
            if state == QUEUED:
                task = self._adopt_persisted(row, state)
                if task is not None:
                    self._record_event(
                        task_id, "-", 0, QUEUED, "recovered: re-queued")
                continue
            # RUNNING / CANCELLING from a dead process: the worker is
            # gone. The attempt can never confirm, so fence it.
            if state == CANCELLING:
                final = CANCELLED
                reason = "worker lost during cancellation (restart)"
            elif attempts < max_attempts:
                final = QUEUED
                reason = "interrupted at generation %d; re-queued" % (
                    attempts + 1)
            else:
                final = FAILED
                reason = "interrupted by restart; attempts exhausted"
            task = self._adopt_persisted(row, final, reason=reason)
            if task is not None and final in (QUEUED, FAILED, CANCELLED):
                self._record_event(
                    task_id, "-", attempts, final, reason)

    def _adopt_persisted(self, row: Any, state: str,
                         reason: str = "") -> Optional[ScheduledTask]:
        """Load one persisted task row into the live graph (if not
        already present from an explicit add_task)."""
        task_id = row["task_id"]
        if task_id in self._tasks:
            return None
        try:
            deps = tuple(json.loads(row["dependencies"] or "[]"))
            resources = tuple(json.loads(row["resources"] or "[]"))
            timeout = row["timeout"]
            timeout = float(timeout) if timeout is not None else None
        except (ValueError, TypeError):
            # Corrupt row: fail the task closed instead of guessing.
            self._record_event(task_id, "-", 0, FAILED,
                               "corrupt persisted task row")
            return None
        task = ScheduledTask(
            task_id=task_id, description="", depends_on=deps,
            timeout=timeout,
            max_attempts=int(row["max_attempts"] or 1),
            resources=resources, priority=0,
            state=state, attempts_used=int(row["attempts"] or 0),
            error=reason)
        self._tasks[task_id] = task
        return task

    def _dispatch(self, running: Dict[str, _RunningAttempt],
                  pool: ThreadPoolExecutor, executor: ExecutorFn,
                  default_timeout: Optional[float]) -> bool:
        dispatched = False
        for task in sorted(self._tasks.values(),
                           key=lambda t: (-t.priority, t.task_id)):
            if len(running) >= self.max_workers:
                break
            with self._state_lock:
                if task.state != QUEUED:
                    continue
                if self._run_cancelled:
                    self._finalize(task, CANCELLED,
                                   "run cancelled before start")
                    continue
                if task.task_id in self._cancel_requested:
                    self._finalize(task, CANCELLED,
                                   "cancelled by operator before start")
                    continue
                # Dependency gate: every dependency must have SUCCEEDED.
                blocked = ""
                for dep in task.depends_on:
                    dep_task = self._tasks.get(dep)
                    if dep_task is None or dep_task.state != SUCCEEDED:
                        blocked = dep or "unknown"
                        break
                if blocked:
                    if self._tasks[blocked].state in TERMINAL_STATES:
                        self._finalize(
                            task, SKIPPED,
                            "dependency %s ended %s"
                            % (blocked, self._tasks[blocked].state))
                        continue
                    continue
                # Resource lock gate: deterministic conflict, no waiting.
                conflict = None
                for resource in task.resources:
                    holder = self._lock_holders.get(resource)
                    if holder is not None and holder != task.task_id:
                        conflict = (resource, holder)
                        break
                if conflict is not None:
                    self._finalize(
                        task, FAILED,
                        "LOCK_CONFLICT: resource %r held by task %s"
                        % (conflict[0], conflict[1]))
                    continue
                fence = self.registry.begin(
                    task.task_id, owner=self.owner or "scheduler")
                control = SupervisorControl()
                guard = _guard_for(fence, self.registry)
                self._controls[task.task_id] = control
                task.state = RUNNING
                task.attempts_used += 1
                self._persist_task(task)
                for resource in task.resources:
                    self._lock_holders[resource] = task.task_id
                self._record_event(
                    task.task_id, fence.attempt_id, fence.generation,
                    RUNNING, "attempt %d started" % fence.attempt)
                timeout = task.timeout if task.timeout is not None \
                    else default_timeout
                deadline = (time.time() + float(timeout)
                            if timeout else None)
                future = pool.submit(self._worker, task, fence, control,
                                     guard, executor)
                running[task.task_id] = self._RunningAttempt(
                    task_id=task.task_id, fence=fence, control=control,
                    guard=guard, future=future, deadline=deadline,
                    dispatched_at=time.time())
                dispatched = True
        return dispatched

    def _worker(self, task: ScheduledTask, fence: AttemptFence,
                control: SupervisorControl, guard: Callable[[], str],
                executor: ExecutorFn) -> Dict[str, Any]:
        """Worker body: run the executor, then commit — fenced attempts
        never land their result."""
        outcome: Dict[str, Any] = {
            "success": False, "output": "", "error": "",
            "cancelled": False, "terminal": False, "metadata": {}}
        try:
            raw = executor(task, fence, control, guard)
            if not isinstance(raw, dict):
                raise DAGSchedulerError(
                    "executor for %s returned %r, not a dict"
                    % (task.task_id, type(raw).__name__))
            outcome["success"] = bool(raw.get("success"))
            outcome["output"] = str(raw.get("output") or "")[:4000]
            outcome["error"] = str(raw.get("error") or "")[:MAX_TASK_ERROR]
            outcome["cancelled"] = bool(raw.get("cancelled"))
            outcome["terminal"] = bool(raw.get("terminal",
                                               outcome["success"]
                                               or outcome["cancelled"]))
            outcome["metadata"] = {
                key: value for key, value in (raw.get("metadata") or {}).items()
                if isinstance(key, str)}
        except TaskCancelled as exc:
            outcome["error"] = str(exc) or "cancelled by operator"
            outcome["cancelled"] = True
            outcome["terminal"] = True
        except Exception as exc:
            outcome["error"] = "%s: %s" % (type(exc).__name__, exc)
            outcome["terminal"] = True
        # Commit — this is where a stale attempt dies.
        if outcome["cancelled"]:
            terminal = CANCELLED
        elif outcome["success"]:
            terminal = SUCCEEDED
        else:
            terminal = FAILED
        try:
            if outcome["cancelled"]:
                # Confirm the cooperative cancel (CANCELLING → CANCELLED);
                # a direct cancel commits from RUNNING/QUEUED.
                try:
                    self.registry.confirm_cancelled(
                        task.task_id, fence,
                        reason=outcome["error"])
                except Exception:
                    self.registry.commit(
                        task.task_id, fence, CANCELLED,
                        payload={"output": outcome["output"],
                                 "error": outcome["error"]})
            else:
                self.registry.commit(
                    task.task_id, fence, terminal,
                    payload={"output": outcome["output"],
                             "error": outcome["error"]})
        except StaleAttemptError as exc:
            # The scheduler already fenced this attempt (timeout/cancel/
            # restart/supersede). Its result is rejected, full stop.
            outcome["rejected_stale"] = True
            self._persist_attempt(
                task.task_id, fence.generation, FENCED,
                "stale result rejected: %s" % exc,
                fence.created_at, time.time(), outcome["output"],
                outcome["error"])
            self._record_event(
                task.task_id, fence.attempt_id, fence.generation, FENCED,
                "stale worker result rejected: %s" % exc)
            self._emit(task.task_id, {
                "event": "stale_result_rejected", "state": FENCED,
                "attempt_id": fence.attempt_id, "generation": fence.generation,
                "detail": str(exc)[:500]})
            return outcome
        except Exception as exc:  # state machine conflict: fail closed
            self._record_event(
                task.task_id, fence.attempt_id, fence.generation, FAILED,
                "commit conflict: %s" % exc)
            terminal = FAILED
            outcome["error"] = outcome["error"] or str(exc)
        self._persist_attempt(
            task.task_id, fence.generation, terminal,
            outcome["error"][:200], fence.created_at, time.time(),
            outcome["output"], outcome["error"])
        return outcome

    def _sweep(self, running: Dict[str, _RunningAttempt]) -> None:
        """Watchdog pass: time out expired attempts, confirm or fence
        unconfirmed cancellations, release locks of finished threads."""
        now = time.time()
        for task_id, attempt in list(running.items()):
            task = self._tasks.get(task_id)
            if task is None:
                continue
            with self._state_lock:
                # A finished future: process the outcome (commit already
                # happened in the worker; here we reconcile task state
                # and schedule retries).
                if attempt.future.done():
                    outcome = _safe_result(attempt.future)
                    self._release_locks(task)
                    del running[task_id]
                    self._handle_outcome(task, attempt, outcome)
                    continue
                # Timeout: fence the attempt now; the thread may still be
                # alive but is no longer authorized to commit or write.
                # The task row stays RUNNING until the thread's fate is
                # known (a restart during that window is handled by
                # _recover).
                if attempt.deadline is not None \
                        and now >= attempt.deadline \
                        and attempt.abandon_at is None \
                        and task.state == RUNNING:
                    fence = attempt.fence
                    try:
                        self.registry.timeout(
                            task_id, fence,
                            reason="exceeded %.3gs" % (
                                attempt.deadline
                                - attempt.dispatched_at))
                        self.registry.fence(
                            task_id, fence, reason="timeout")
                    except Exception:
                        pass  # already fenced by cancel/restart
                    task.error = "attempt %d timed out" % fence.attempt
                    self._persist_task(task)
                    self._persist_attempt(
                        task_id, fence.generation, FENCED, "timeout",
                        fence.created_at, now, "",
                        "attempt timed out")
                    self._record_event(
                        task_id, fence.attempt_id, fence.generation, FENCED,
                        "attempt timed out; fenced")
                    attempt.control.request_cancel()
                    # Wait for the thread to actually end before retrying.
                    attempt.abandon_at = now + self.abandon_wait
                # Abandoned-thread budget: the fenced thread did not
                # finish in time. Do not overlap it; end the task
                # deterministically.
                if attempt.abandon_at is not None \
                        and now >= attempt.abandon_at:
                    del running[task_id]
                    self._release_locks(task)
                    if task.attempts_used < task.max_attempts:
                        # Still no safe moment to retry: the previous
                        # attempt is still executing. Fail closed rather
                        # than overlap it.
                        self._finalize(
                            task, FENCED,
                            "attempt %d abandoned (still running after "
                            "timeout); retry refused to overlap it"
                            % task.attempts_used)
                    else:
                        self._finalize(
                            task, FENCED,
                            "all attempts timed out; last attempt "
                            "abandoned")
                    continue
                # Cancellation: the worker confirms at a checkpoint;
                # silence past the grace means fence + record CANCELLED.
                if task.task_id in self._cancel_requested \
                        and task.state == CANCELLING \
                        and attempt.cancel_at is None:
                    attempt.cancel_at = now + self.cancel_grace
                if attempt.cancel_at is not None \
                        and now >= attempt.cancel_at \
                        and task.state == CANCELLING:
                    del running[task_id]
                    self._release_locks(task)
                    fence = attempt.fence
                    try:
                        if fence.state == CANCELLING:
                            self.registry.fence(
                                task_id, fence,
                                reason="worker did not confirm "
                                       "cancellation")
                    except Exception:
                        pass
                    self._finalize(
                        task, CANCELLED,
                        "worker did not confirm cancellation; attempt "
                        "fenced")
                    continue

    def _handle_outcome(self, task: ScheduledTask,
                        attempt: _RunningAttempt,
                        outcome: Dict[str, Any]) -> None:
        """Reconcile one completed (and committed-or-rejected) attempt
        with the task's terminal state or next retry."""
        fence = attempt.fence
        fence_state = self.registry.current(task.task_id)
        fence_state = fence_state.state if fence_state is not None else FENCED
        if outcome.get("rejected_stale"):
            # Worker reported the commit was rejected; the task state was
            # already driven by the watchdog (FENCED/TIMED_OUT path).
            if task.attempts_used < task.max_attempts:
                self._requeue(task, "retry after fenced attempt")
            else:
                self._finalize(task, FENCED,
                               "all attempts fenced (stale results)")
            return
        if outcome.get("cancelled"):
            if fence_state == CANCELLED or task.state in (CANCELLED,
                                                          CANCELLING):
                self._finalize(
                    task, CANCELLED,
                    outcome.get("error") or "cancelled by operator")
            else:
                # Cancelled after the task was already fenced by timeout:
                # timeout wins (it happened first).
                if task.attempts_used < task.max_attempts:
                    self._requeue(task, "retry after fenced attempt")
                else:
                    self._finalize(task, FENCED,
                                   "all attempts fenced")
            return
        if outcome.get("success"):
            self._finalize(
                task, SUCCEEDED, "",
                output=outcome.get("output", ""),
                metadata=outcome.get("metadata", {}))
            return
        # Failure: retry while the budget allows and the failure is not
        # terminal (the executor marks unretryable failures terminal).
        if outcome.get("terminal") or task.attempts_used >= task.max_attempts:
            self._finalize(
                task, FAILED, outcome.get("error") or "task failed",
                output=outcome.get("output", ""),
                metadata=outcome.get("metadata", {}))
            return
        self._requeue(task, "retry after failure: %s"
                      % (outcome.get("error") or ""))

    def _requeue(self, task: ScheduledTask, reason: str) -> None:
        task.state = QUEUED
        task.error = ""
        self._persist_task(task)
        self._record_event(
            task.task_id, "-", task.attempts_used, QUEUED,
            "attempt %d re-queued (%s)" % (task.attempts_used + 1,
                                           reason[:200]))

    def _finalize(self, task: ScheduledTask, state: str, reason: str,
                  output: str = "", metadata: Optional[dict] = None) -> None:
        if task.state in TERMINAL_STATES and task.state != state:
            return  # a different terminal already won; it is durable
        fence = self.registry.current(task.task_id)
        if fence is not None and fence.state not in TERMINAL_STATES:
            # Close the attempt's state machine so no late commit can
            # land after we recorded the task terminal.
            try:
                if state in (SUCCEEDED, FAILED, CANCELLED):
                    self.registry.commit(task.task_id, fence, state,
                                         payload={"note": reason[:300]})
                else:  # FENCED / SKIPPED
                    self.registry.fence(task.task_id, fence,
                                        reason=reason[:300])
            except Exception:
                # Already fenced or conflicted: the task record below is
                # the durable truth; the attempt is fenced either way.
                pass
        task.state = state
        task.error = (reason or "")[:MAX_TASK_ERROR]
        task.output = (output or "")[:4000]
        if metadata:
            task.result_metadata = dict(metadata)
        self._persist_task(task)
        self._release_locks(task)
        self._record_event(
            task.task_id, fence.attempt_id if fence is not None else "-",
            fence.generation if fence is not None else 0, state,
            reason[:300] or state)

    def _release_locks(self, task: ScheduledTask) -> None:
        for resource in list(self._lock_holders):
            if self._lock_holders[resource] == task.task_id:
                del self._lock_holders[resource]

    def _resolve_unschedulable(self) -> None:
        """Make every non-terminal task deterministic when the dispatch
        loop is stalled (dependencies failed, locks held by dead tasks,
        or a run-level cancel)."""
        for task in sorted(self._tasks.values(), key=lambda t: t.task_id):
            if task.state in TERMINAL_STATES:
                continue
            if task.state == RUNNING:
                continue  # still being processed by the sweep
            if task.task_id in self._cancel_requested or self._run_cancelled:
                self._finalize(task, CANCELLED, "cancelled by operator")
                continue
            blocked = ""
            for dep in task.depends_on:
                dep_task = self._tasks.get(dep)
                if dep_task is None or dep_task.state != SUCCEEDED:
                    blocked = dep or "unknown"
                    break
            if blocked:
                dep_state = (self._tasks[blocked].state
                             if blocked in self._tasks else "missing")
                self._finalize(
                    task, SKIPPED,
                    "dependency %s ended %s" % (blocked, dep_state))
                continue
            self._finalize(
                task, FAILED,
                "LOCK_CONFLICT: a requested resource lock is unavailable")

    def _all_terminal(self) -> bool:
        return all(t.state in TERMINAL_STATES for t in self._tasks.values())

    def _wait(self, running: Dict[str, _RunningAttempt]) -> None:
        futures = [attempt.future for attempt in running.values()]
        if not futures:
            return
        done, _ = wait(futures, timeout=0.05, return_when=FIRST_COMPLETED)
        del done


def _safe_result(future: Future) -> Dict[str, Any]:
    try:
        outcome = future.result()
        if not isinstance(outcome, dict):
            return {"success": False, "terminal": True,
                    "error": "executor returned non-dict result",
                    "output": "", "cancelled": False, "metadata": {},
                    "rejected_stale": False}
        return dict(outcome)
    except Exception as exc:  # the worker catches everything; defensive
        return {"success": False, "terminal": True,
                "error": "%s: %s" % (type(exc).__name__, exc),
                "output": "", "cancelled": False, "metadata": {},
                "rejected_stale": False}


def _guard_for(fence: AttemptFence, registry: FenceRegistry) -> Callable[[], str]:
    from forge.core.fencing import commit_guard

    return commit_guard(fence, registry)
