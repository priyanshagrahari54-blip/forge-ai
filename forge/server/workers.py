"""Background workers: tasks keep running without any client (A81).

Workers are threads in a bounded pool owned by the server process — not
by any HTTP connection. A Forge Desktop client can submit a task and
disconnect; the scheduler keeps leasing queued work, the workers keep
executing it, and every step is persisted (status, stage, progress,
events, logs, approvals, results). Reconnecting recovers the full
picture; nothing waits on a socket.

Crash safety is lease-based: a worker re-validates its queue lease
before committing any terminal mutation, so after a restart a zombie
worker from the previous process discards its outcome instead of
clobbering the fresh one.

Failure handling is honest and bounded: an executor exception retries
with backoff until ``max_retries`` is exhausted, then the task fails
with the captured error. A Supervisor *rejection* (tests failed, review
vetoed, acceptance denied) is a deterministic outcome — it fails the
task immediately instead of looping, exactly like the guarded pipeline
itself.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional, Set

from forge.core.report import redact
from forge.core.run_control import SupervisorControl, TaskCancelled
from forge.server.errors import ServerError, VersionConflict
from forge.server.executor import ExecutionContext
from forge.server.models import TaskStatus


class WorkerPool:
    """Bounded thread pool with liveness and busy accounting."""

    def __init__(self, max_workers: int = 4) -> None:
        self.max_workers = max(1, int(max_workers))
        self._executor: Optional[ThreadPoolExecutor] = None
        self._busy = 0
        self._lock = threading.Lock()
        self._pending: Set[Future] = set()

    def start(self) -> None:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self.max_workers,
                    thread_name_prefix="forge-worker")

    def submit(self, fn: Callable[..., Any], *args: Any) -> Future:
        with self._lock:
            if self._executor is None:
                raise RuntimeError("Worker pool is not running")
            executor = self._executor

        def wrapped() -> Any:
            with self._lock:
                self._busy += 1
            try:
                return fn(*args)
            finally:
                with self._lock:
                    self._busy -= 1

        future = executor.submit(wrapped)
        self._pending.add(future)
        future.add_done_callback(self._pending.discard)
        return future

    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            executor = self._executor
            self._executor = None
            pending = list(self._pending)
            self._pending.clear()
        if executor is None:
            return
        for future in pending:
            # Queued-but-unstarted work is dropped; running work finishes
            # (or is abandoned when wait=False). No cancel_futures kwarg:
            # it is 3.9+ and this must run on 3.8.
            future.cancel()
        executor.shutdown(wait=wait)

    @property
    def alive(self) -> bool:
        return self._executor is not None

    @property
    def busy(self) -> int:
        with self._lock:
            return self._busy


class _LeaseLost(Exception):
    """Internal: this worker's queue lease no longer exists (restart fence)."""


def run_task(server: Any, task_id: str, owner: str,
             lease_project_id: str = "") -> None:
    """Execute one leased task end to end (the worker body).

    Never raises: every failure becomes task state (retried, failed, or
    cancelled) plus events and logs. ``owner`` is the lease token the
    scheduler used; it fences terminal mutations against restarts.
    ``lease_project_id`` is the scheduler's accounting key, so the
    active counter is released even when the task row has vanished.
    """
    project_id = lease_project_id
    try:
        task = server.tasks.get(task_id)
        if task is None:
            server.queue.release(task_id)
            return
        project_id = task.project_id or lease_project_id
        if task.status != TaskStatus.QUEUED:
            # Cancelled/paused between enqueue and lease: hand the slot
            # back (or drop it for terminal tasks) without executing.
            if task.terminal:
                server.queue.release(task_id)
            else:
                server.queue.requeue(task_id)
            return
        if not server.queue.lease_held_by(task_id, owner):
            return
        try:
            task = server.tasks.transition(
                task_id, TaskStatus.STARTED, expected=(TaskStatus.QUEUED,))
        except (ServerError, VersionConflict):
            server.queue.requeue(task_id)
            return
        server.emit(task_id, project_id, "task.started",
                    {"mode": task.mode, "owner": owner,
                     "attempt": task.retry_count + 1})
        server.log(task_id, project_id,
                   "Worker picked up task (attempt %d of %d)."
                   % (task.retry_count + 1, task.max_retries + 1),
                   source="worker")

        control = SupervisorControl()
        server.register_control(task_id, control)
        control.on_pause_state(
            lambda paused: _on_pause_state(server, task_id, paused))

        checkpoint_id = _ensure_checkpoint(server, task)
        if checkpoint_id is None:
            return  # task already failed by _ensure_checkpoint

        task = server.tasks.transition(
            task_id, TaskStatus.RUNNING, expected=(TaskStatus.STARTED,))
        server.emit(task_id, project_id, "task.running",
                    {"checkpoint_id": checkpoint_id})

        ctx = ExecutionContext(server, task, 
                               server.projects.get_or_raise(project_id),
                               control, checkpoint_id)
        try:
            outcome = server.executor.execute(ctx)
        except TaskCancelled as exc:
            _finish_cancelled(server, task_id, project_id, owner, str(exc))
            return
        except Exception as exc:  # infrastructure failure → bounded retry
            _finish_failed(server, task_id, project_id, owner,
                           "Worker error: %s" % exc, retry=True)
            return

        if not server.queue.lease_held_by(task_id, owner):
            # A restart happened mid-run: our lease is gone and the task
            # has been re-queued (or failed) by recovery. Discard.
            server.log(task_id, project_id,
                       "Lease lost after server restart; discarding "
                       "stale worker outcome.", level="warning",
                       source="worker")
            return
        if control.cancel_requested or outcome.get("cancelled"):
            _finish_cancelled(server, task_id, project_id, owner,
                              "Cancelled by operator.",
                              files=outcome.get("files"))
            return
        task = server.tasks.get_or_raise(task_id)
        if outcome.get("accepted"):
            _finish_completed(server, task, owner, outcome)
        else:
            _finish_failed(server, task_id, project_id, owner,
                           str(outcome.get("error") or
                               "Task rejected by the pipeline."),
                           retry=False, outcome=outcome)
    except Exception as exc:  # pragma: no cover - defensive last resort
        try:
            server.log(task_id, project_id,
                       "Unhandled worker error: %s" % exc,
                       level="error", source="worker")
            _finish_failed(server, task_id, project_id, owner,
                           "Unhandled worker error: %s" % exc, retry=False)
        except Exception:
            pass
    finally:
        server.unregister_control(task_id)
        server.scheduler.task_finished(lease_project_id or project_id)


# -- worker helpers -----------------------------------------------------------

def _ensure_checkpoint(server: Any, task: Any) -> Optional[str]:
    """Hold a pre-run checkpoint for the task (reused across retries)."""
    task_id = task.task_id
    project_id = task.project_id
    existing = server.held_checkpoint(task_id)
    if existing is not None:
        return task.checkpoint_id or getattr(existing, "id", "")
    try:
        from forge.tools.checkpoint import CheckpointManager

        project = server.projects.get_or_raise(project_id)
        checkpoint = CheckpointManager(project.root).create(
            "forge-server-%s" % task_id)
    except Exception as exc:
        server.tasks.transition(
            task_id, TaskStatus.FAILED,
            expected=(TaskStatus.STARTED, TaskStatus.QUEUED),
            error="Pre-run checkpoint failed: %s" % exc)
        server.emit(task_id, project_id, "task.failed",
                    {"reason": "checkpoint_failed", "error": str(exc)[:500]})
        server.log(task_id, project_id,
                   "Pre-run checkpoint failed: %s" % exc,
                   level="error", source="worker")
        server.queue.release(task_id)
        server.notify(project_id, "task.failed",
                      "Task %s failed" % task_id,
                      "Pre-run checkpoint failed.", task_id=task_id)
        return None
    server.hold_checkpoint(task_id, checkpoint)
    server.tasks.update(task_id, checkpoint_id=str(checkpoint.id))
    server.emit(task_id, project_id, "checkpoint.created",
                {"checkpoint_id": checkpoint.id})
    return str(checkpoint.id)


def _fenced(server: Any, task_id: str, owner: str) -> bool:
    return server.queue.lease_held_by(task_id, owner)


def _finish_completed(server: Any, task: Any, owner: str,
                      outcome: Dict[str, Any]) -> None:
    task_id = task.task_id
    project_id = task.project_id
    if not _fenced(server, task_id, owner):
        return
    result = outcome.get("result") or {}
    if not isinstance(result, dict):
        result = {"value": result}
    payload = json.dumps(redact(result), default=str)
    server.tasks.transition(
        task_id, TaskStatus.COMPLETED,
        expected=(TaskStatus.RUNNING, TaskStatus.STARTED,
                  TaskStatus.WAITING_FOR_APPROVAL, TaskStatus.PAUSED),
        result_json=payload, error="", stage="completed", progress=1.0)
    server.emit(task_id, project_id, "task.completed",
                {"files": outcome.get("files", []),
                 "model": outcome.get("model", ""),
                 "provider": outcome.get("provider", "")})
    server.log(task_id, project_id, "Task completed successfully.",
               source="worker")
    server.notify(project_id, "task.completed",
                  "Task %s completed" % task_id,
                  "%d file(s) changed." % len(outcome.get("files", [])),
                  task_id=task_id)
    _terminal_cleanup(server, task_id, project_id, owner)
    server.record_task_memory(task_id)


def _finish_failed(server: Any, task_id: str, project_id: str, owner: str,
                   error: str, *, retry: bool,
                   outcome: Optional[Dict[str, Any]] = None) -> None:
    if not _fenced(server, task_id, owner):
        return
    task = server.tasks.get(task_id)
    if task is None or task.terminal:
        _terminal_cleanup(server, task_id, project_id, owner)
        return
    if task.cancel_requested:
        _finish_cancelled(server, task_id, project_id, owner,
                          "Cancelled by operator.")
        return
    error_text = str(error)[:2000]
    if retry and task.retry_count < task.max_retries:
        server.tasks.update(task_id, retry_count=task.retry_count + 1)
        server.tasks.transition(
            task_id, TaskStatus.QUEUED,
            expected=(TaskStatus.RUNNING, TaskStatus.STARTED,
                      TaskStatus.PAUSED, TaskStatus.WAITING_FOR_APPROVAL),
            stage="retry_scheduled", error=error_text)
        delay = max(0.0, float(server.config.retry_backoff_seconds))
        server.queue.requeue(task_id, available_at=time.time() + delay)
        server.emit(task_id, project_id, "task.retrying",
                    {"error": error_text,
                     "retry_count": task.retry_count + 1,
                     "max_retries": task.max_retries,
                     "backoff_seconds": delay})
        server.log(task_id, project_id,
                   "Execution failed (%s); retrying (%d/%d) in %.1fs."
                   % (error_text[:200], task.retry_count + 1,
                      task.max_retries, delay),
                   level="warning", source="worker")
        server.scheduler.wake()
        return
    updates: Dict[str, Any] = {"error": error_text, "stage": "failed"}
    if outcome is not None:
        result = outcome.get("result") or {}
        if isinstance(result, dict) and result:
            updates["result_json"] = json.dumps(
                redact(result), default=str)
    server.tasks.transition(
        task_id, TaskStatus.FAILED,
        expected=(TaskStatus.RUNNING, TaskStatus.STARTED, TaskStatus.QUEUED,
                  TaskStatus.PAUSED, TaskStatus.WAITING_FOR_APPROVAL),
        **updates)
    server.emit(task_id, project_id, "task.failed",
                {"error": error_text, "retry_count": task.retry_count})
    server.log(task_id, project_id, "Task failed: %s" % error_text[:500],
               level="error", source="worker")
    server.notify(project_id, "task.failed", "Task %s failed" % task_id,
                  error_text[:500], task_id=task_id)
    _terminal_cleanup(server, task_id, project_id, owner)
    server.record_task_memory(task_id)


def _finish_cancelled(server: Any, task_id: str, project_id: str,
                      owner: str, reason: str,
                      files: Any = None) -> None:
    if not _fenced(server, task_id, owner):
        return
    task = server.tasks.get(task_id)
    if task is None or task.terminal:
        _terminal_cleanup(server, task_id, project_id, owner)
        return
    rolled_back = False
    changed = [str(path) for path in (files or []) if isinstance(path, str)]
    checkpoint = server.held_checkpoint(task_id)
    if checkpoint is not None:
        # The Supervisor rolls its own candidate set back on
        # cancellation (this restore is then a hash-verified no-op);
        # for other executors it restores the server's pre-run
        # checkpoint so a cancelled task leaves no half-applied state.
        try:
            from forge.tools.checkpoint import CheckpointManager

            project = server.projects.get_or_raise(project_id)
            CheckpointManager(project.root).rollback(
                checkpoint, changed or None)
            rolled_back = True
        except Exception as exc:
            server.log(task_id, project_id,
                       "Cancellation rollback failed: %s" % exc,
                       level="error", source="worker")
    server.tasks.transition(
        task_id, TaskStatus.CANCELLED,
        expected=(TaskStatus.RUNNING, TaskStatus.STARTED, TaskStatus.QUEUED,
                  TaskStatus.PAUSED, TaskStatus.WAITING_FOR_APPROVAL),
        error=reason[:2000], stage="cancelled",
        result_json=json.dumps({"cancelled": True,
                                "rollback": rolled_back}))
    server.emit(task_id, project_id, "task.cancelled",
                {"reason": reason[:500], "rollback": rolled_back})
    server.log(task_id, project_id, "Task cancelled: %s" % reason[:200],
               source="worker")
    server.notify(project_id, "task.cancelled",
                  "Task %s cancelled" % task_id, reason[:200],
                  task_id=task_id)
    _terminal_cleanup(server, task_id, project_id, owner)
    server.record_task_memory(task_id)


def _terminal_cleanup(server: Any, task_id: str, project_id: str,
                      owner: str) -> None:
    """Release the lease/queue slot and wake the scheduler.

    The per-project active counter is decremented exactly once per
    lease, in :func:`run_task`'s ``finally`` — never here.
    """
    del owner, project_id
    server.queue.release(task_id)
    server.expire_task_approvals(task_id)
    server.scheduler.wake()


def _on_pause_state(server: Any, task_id: str, paused: bool) -> None:
    """Mirror the cooperative pause flag into durable task status."""
    try:
        task = server.tasks.get(task_id)
        if task is None or task.terminal:
            return
        if paused and task.status in (TaskStatus.RUNNING,
                                      TaskStatus.STARTED,
                                      TaskStatus.WAITING_FOR_APPROVAL):
            server.tasks.transition(task_id, TaskStatus.PAUSED,
                                    expected=(task.status,))
            server.emit(task_id, task.project_id, "task.paused",
                        {"detail": "Paused at a stage boundary."})
            server.log(task_id, task.project_id,
                       "Paused at a stage boundary.", source="worker")
        elif not paused and task.status == TaskStatus.PAUSED:
            server.tasks.transition(task_id, TaskStatus.RUNNING,
                                    expected=(TaskStatus.PAUSED,))
            server.emit(task_id, task.project_id, "task.resumed", {})
            server.log(task_id, task.project_id, "Resumed.",
                       source="worker")
    except Exception:
        pass  # observability must never break enforcement
