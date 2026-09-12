"""Bounded, conflict-aware parallel task scheduling (A81).

:class:`ParallelTaskScheduler` is the supervisor's execution engine. It
answers, on every loop iteration, which tasks may run *right now*:

* **dependencies** — a task is ready only when every dependency
  succeeded;
* **conflicts** — a ready task is never dispatched while a running task
  writes the same file, shares an exclusive resource, or is on the
  same sequential lane (serialized instead, with an event);
* **locks** — admission requires the task's full lock set to be granted
  atomically (``try_acquire``), so a task never holds one lock while
  blocked on another and deadlocks are structurally impossible on this
  path;
* **priority** — among dispatchable tasks, a priority queue
  (priority desc, created_at, id) decides the order;
* **bounded concurrency** — at most ``max_workers`` tasks run at once.

Failure isolation: when a task fails (after its retries), is denied, or
is cancelled, every transitive dependent is marked ``BLOCKED`` with the
failing dependency named; independent branches keep running and a
failing subtree never poisons the rest of the graph.

Cancellation is cooperative through
:class:`~forge.core.run_control.SupervisorControl`: workers check
checkpoints, the loop stops admitting new work, and pending/blocked
tasks are cancelled.

Every transition is emitted as a structured event (consumed by the
activity tracker and the persistent state store) and every inter-agent
hand-off travels as a validated structured message on the
:class:`~forge.orchestration.messages.MessageBus` — agents never share
uncontrolled state.

Workers are ``Callable[[TaskWorkItem], dict]``. A worker may raise
:class:`TaskDeniedError` (policy denial: terminal, never retried),
:class:`~forge.core.run_control.TaskCancelled` (cancellation), or any
exception (a failed attempt, retried up to the task's ``max_retries``).
Results are validated against the role's result schema and byte-limit
before acceptance.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, \
    wait
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping

from forge.core.run_control import SupervisorControl, TaskCancelled
from forge.orchestration.activity import AgentActivityTracker
from forge.orchestration.graph import (SEQUENTIAL_RESOURCE, TERMINAL_STATES,
                                       TaskGraph, TaskNode, TaskStatus,
                                       UNSUCCESSFUL)
from forge.orchestration.locking import ResourceLockManager
from forge.orchestration.messages import MessageBus
from forge.orchestration.roles import get_role_spec, validate_result
from forge.orchestration.state import ExecutionStateStore

SUPERVISOR_IDENTITY = "forge-supervisor"


class TaskError(Exception):
    """A task attempt failed (retryable)."""


class TaskDeniedError(Exception):
    """A task was denied by policy: terminal, never retried."""


class TaskTimeoutError(TaskError):
    """A task attempt exceeded its wall-clock budget."""


@dataclass
class TaskWorkItem:
    """Structured work handed to a role worker."""

    run_id: str
    task: TaskNode
    context: list[dict[str, Any]] = field(default_factory=list)
    checkpoint: Callable[[], None] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task.id,
            "role": self.task.role,
            "description": self.task.description,
            "reads": sorted(self.task.reads),
            "writes": sorted(self.task.writes),
            "resources": sorted(self.task.resources),
            "kind": self.task.kind.value,
            "priority": self.task.priority,
            "context": list(self.context),
        }


# Runtime type alias (not an annotation), so it must use typing.Dict:
# ``dict[str, Any]`` is evaluated at import and is not subscriptable on
# Python 3.8.
Worker = Callable[[TaskWorkItem], Dict[str, Any]]


@dataclass
class ExecutionReport:
    run_id: str
    status: str
    summary: str
    tasks: list[TaskNode]
    messages: list[dict[str, Any]]
    events: list[dict[str, Any]]
    counts: dict[str, int]
    started_at: float
    finished_at: float

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    @property
    def succeeded(self) -> bool:
        return self.status == "SUCCEEDED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "summary": self.summary,
            "succeeded": self.succeeded,
            "counts": dict(self.counts),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "tasks": [task.to_dict() for task in self.tasks],
            "messages": list(self.messages),
            "events": list(self.events),
        }


class ParallelTaskScheduler:
    """Executes one :class:`TaskGraph` with bounded, conflict-aware
    concurrency under supervisor control."""

    def __init__(self, graph: TaskGraph, workers: Mapping[str, Worker], *,
                 max_workers: int = 4,
                 run_id: str = "",
                 requirement: str = "",
                 project: str = "",
                 state_store: ExecutionStateStore | None = None,
                 control: SupervisorControl | None = None,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None,
                 lock_manager: ResourceLockManager | None = None,
                 message_bus: MessageBus | None = None,
                 activity: AgentActivityTracker | None = None,
                 retry_backoff: float = 0.0,
                 cancel_wait: float = 10.0) -> None:
        graph.validate()
        missing = {task.role for task in graph.tasks} - set(workers)
        if missing:
            raise ValueError(
                "No worker registered for roles: " + ", ".join(sorted(missing)))
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if retry_backoff < 0:
            raise ValueError("retry_backoff must be non-negative")
        self.graph = graph
        self.workers = dict(workers)
        self.max_workers = max_workers
        self.run_id = run_id
        self.requirement = requirement
        self.project = project
        self.state_store = state_store
        self.control = control
        self.on_event = on_event
        self.locks = lock_manager or ResourceLockManager()
        self.activity = activity or AgentActivityTracker()
        self.retry_backoff = retry_backoff
        self.cancel_wait = cancel_wait
        roles = {task.role for task in graph.tasks}
        self.message_bus = message_bus or MessageBus(
            roles=roles | {SUPERVISOR_IDENTITY})
        self._wake = threading.Event()
        self._events: list[dict[str, Any]] = []

    # -- operator actions -----------------------------------------------------

    def cancel(self) -> None:
        if self.control is not None:
            self.control.request_cancel()
        self._wake.set()

    def block(self, task_id: str, reason: str = "") -> None:
        task = self.graph.get(task_id)
        if task.status == TaskStatus.RUNNING:
            raise ValueError(
                f"Cannot block running task {task_id!r}; cancel the run "
                f"instead")
        self.graph.block(task_id, reason)
        self._emit("task_blocked", {"task_id": task_id, "role": task.role,
                                    "reason": task.blocked_reason})
        if self.state_store is not None:
            self.state_store.save_task(self.run_id, task)
        self._wake.set()

    def unblock(self, task_id: str) -> None:
        task = self.graph.unblock(task_id)
        self._emit("task_unblocked", {"task_id": task_id, "role": task.role})
        if self.state_store is not None:
            self.state_store.save_task(self.run_id, task)
        self._wake.set()

    def snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "graph": self.graph.summary(),
            "tasks": [task.to_dict() for task in self.graph.tasks],
            "activity": self.activity.snapshot(),
            "locks": self.locks.snapshot(),
        }

    # -- main loop ------------------------------------------------------------

    def run(self) -> ExecutionReport:
        started = time.time()
        run_id = self.run_id or uuid.uuid4().hex
        self.run_id = run_id
        self._emit("run_started", {
            "run_id": run_id, "requirement": self.requirement,
            "tasks": [task.id for task in self.graph.tasks]})
        if self.state_store is not None:
            self.state_store.create_run(run_id, self.project,
                                        self.requirement, {
                "max_workers": self.max_workers,
                "retry_backoff": self.retry_backoff,
                "roles": sorted({task.role for task in self.graph.tasks}),
            })
        for task in self.graph.tasks:
            self._emit("task_queued", {
                "task_id": task.id, "role": task.role,
                "priority": task.priority, "at": time.time()})

        futures: dict[str, Future] = {}
        cancelled = False
        with ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix="forge-task") as pool:
            while True:
                self._propagate_failures()

                if self.control is not None and \
                        self.control.cancel_requested:
                    cancelled = True
                    self._cancel_remaining()
                    # Collect in-flight work: cooperative workers stop at
                    # their next checkpoint; non-cooperative ones are
                    # cancelled after the cancel_wait grace period.
                    for step_id, future in list(futures.items()):
                        done, _ = wait((future,),
                                        timeout=self.cancel_wait)
                        if done:
                            self._finish_task(step_id, future)
                        else:
                            task = self.graph.get(step_id)
                            task.status = TaskStatus.CANCELLED
                            task.error = (
                                "cancelled by operator "
                                "(worker did not stop in time; "
                                "result discarded)")
                            task.finished_at = time.time()
                            self.locks.release(step_id, force=True)
                            self._emit("task_finished", {
                                "task_id": task.id, "role": task.role,
                                "status": task.status.value,
                                "attempts": task.attempts,
                                "error": task.error,
                                "at": task.finished_at})
                            if self.state_store is not None:
                                self.state_store.save_task(
                                    self.run_id, task)
                        futures.pop(step_id, None)
                    break

                self._dispatch(futures, pool)

                if futures:
                    done, _ = wait(tuple(futures.values()), timeout=0.05,
                                   return_when=FIRST_COMPLETED)
                    for step_id, future in list(futures.items()):
                        if future not in done:
                            continue
                        futures.pop(step_id)
                        self._finish_task(step_id, future)
                    # Isolate failures *now*: when several tasks finish in
                    # the same batch, a failure's dependents must be marked
                    # BLOCKED before the exit checks below run — otherwise
                    # the run ends and the final sweep cancels dependents
                    # that failure isolation should have blocked.
                    self._propagate_failures()
                else:
                    self._wake.wait(timeout=0.02)
                    self._wake.clear()

                if all(task.status in TERMINAL_STATES or
                       task.status == TaskStatus.BLOCKED
                       for task in self.graph.tasks):
                    if not futures:
                        break
                if not futures and self._no_progress():
                    break

        # Only PENDING tasks can still be swept here; BLOCKED tasks keep
        # their blocked state and reason (failure isolation is truthful).
        remaining = [task for task in self.graph.tasks
                     if task.status == TaskStatus.PENDING]
        for task in remaining:
            if cancelled:
                task.status = TaskStatus.CANCELLED
                task.error = "cancelled by operator"
            else:
                task.status = TaskStatus.CANCELLED
                task.error = "never scheduled: prerequisites unmet"
            task.finished_at = time.time()
            self._emit("task_finished", {
                "task_id": task.id, "role": task.role,
                "status": task.status.value, "attempts": task.attempts,
                "error": task.error, "at": time.time()})
            if self.state_store is not None:
                self.state_store.save_task(run_id, task)
            self.locks.release(task.id)

        finished = time.time()
        counts: dict[str, int] = {}
        for task in self.graph.tasks:
            counts[task.status.value] = counts.get(task.status.value, 0) + 1
        if cancelled:
            status = "CANCELLED"
        elif counts.get("SUCCEEDED", 0) == len(self.graph.tasks):
            status = "SUCCEEDED"
        elif counts.get("FAILED", 0) or counts.get("DENIED", 0):
            status = "FAILED"
        else:
            status = "PARTIAL"
        summary = (
            f"{counts.get('SUCCEEDED', 0)} of {len(self.graph.tasks)} tasks "
            f"succeeded"
            + (f"; {counts.get('FAILED', 0)} failed"
               if counts.get("FAILED", 0) else "")
            + (f"; {counts.get('DENIED', 0)} denied"
               if counts.get("DENIED", 0) else "")
            + (f"; {counts.get('BLOCKED', 0) + counts.get('SKIPPED', 0)} "
               "blocked"
               if counts.get("BLOCKED", 0) + counts.get("SKIPPED", 0) else "")
            + (f"; {counts.get('CANCELLED', 0)} cancelled"
               if counts.get("CANCELLED", 0) else ""))
        self._emit("run_finished", {
            "run_id": run_id, "status": status, "counts": counts,
            "at": finished})
        if self.state_store is not None:
            self.state_store.update_run(run_id, status, summary, finished)
        self.activity.on_event("run_finished", {
            "status": status, "at": finished})
        return ExecutionReport(
            run_id=run_id, status=status, summary=summary,
            tasks=list(self.graph.tasks),
            messages=self.message_bus.to_list(),
            events=list(self._events), counts=counts,
            started_at=started, finished_at=finished)

    # -- loop steps ---------------------------------------------------------------

    def _no_progress(self) -> bool:
        """True when nothing running can unblock the pending tasks."""
        for task in self.graph.tasks:
            if task.status != TaskStatus.PENDING:
                continue
            if all(self.graph.get(dep).status == TaskStatus.SUCCEEDED
                   for dep in task.dependencies):
                return False  # dispatchable (or about to be)
        return True

    def _propagate_failures(self) -> None:
        """Failure isolation: dependents of unsuccessful tasks are blocked."""
        for task in self.graph.tasks:
            if task.status != TaskStatus.PENDING:
                continue
            for dependency in task.dependencies:
                dep_status = self.graph.get(dependency).status
                if dep_status in UNSUCCESSFUL:
                    task.status = TaskStatus.BLOCKED
                    task.blocked_reason = (
                        f"dependency {dependency} ended {dep_status.value}")
                    task.finished_at = time.time()
                    self._emit("task_blocked", {
                        "task_id": task.id, "role": task.role,
                        "reason": task.blocked_reason, "at": time.time()})
                    if self.state_store is not None:
                        self.state_store.save_task(self.run_id, task)

    def _cancel_remaining(self) -> None:
        for task in self.graph.tasks:
            if task.status in (TaskStatus.PENDING, TaskStatus.BLOCKED):
                task.status = TaskStatus.CANCELLED
                task.error = "cancelled by operator"
                task.finished_at = time.time()
                self._emit("task_finished", {
                    "task_id": task.id, "role": task.role,
                    "status": task.status.value, "attempts": task.attempts,
                    "error": task.error, "at": time.time()})
                if self.state_store is not None:
                    self.state_store.save_task(self.run_id, task)

    def _dispatch(self, futures: dict[str, Future],
                  pool: ThreadPoolExecutor) -> None:
        ready = [task for task in self.graph.tasks
                 if task.status == TaskStatus.PENDING
                 and all(self.graph.get(dep).status == TaskStatus.SUCCEEDED
                         for dep in task.dependencies)]
        ready.sort(key=lambda t: (-t.priority, t.created_at, t.id))
        running = [self.graph.get(task_id) for task_id in futures]
        for task in ready:
            if len(futures) >= self.max_workers:
                break
            conflict_with, reasons = self.graph.conflict_with_running(
                task, running)
            if conflict_with is not None:
                self._emit("conflict_serialized", {
                    "task_id": task.id, "role": task.role,
                    "blocked_by": conflict_with.id, "reasons": list(reasons),
                    "at": time.time()})
                continue
            shared, exclusive = task.lock_keys
            if not self.locks.try_acquire(task.id, shared, exclusive):
                self._emit("lock_wait", {
                    "task_id": task.id, "role": task.role,
                    "keys": sorted(set(shared) | set(exclusive)),
                    "at": time.time()})
                continue
            task.status = TaskStatus.RUNNING
            task.started_at = time.time()
            task.attempts = 0
            self.message_bus.send(
                SUPERVISOR_IDENTITY, task.role, task.id, "task_request",
                content=task.description,
                evidence=(f"kind={task.kind.value}",
                          f"priority={task.priority}"),
                confidence=1.0)
            self._record_message()
            context = [message.to_dict()
                       for message in self.message_bus.for_task(task.id)]
            work = TaskWorkItem(run_id=self.run_id, task=task,
                                context=context,
                                checkpoint=self._checkpoint)
            self._emit("lock_acquired", {
                "task_id": task.id, "role": task.role,
                "keys": sorted(set(shared) | set(exclusive)),
                "at": time.time()})
            self._emit("task_started", {
                "task_id": task.id, "role": task.role, "attempt": 1,
                "at": task.started_at})
            if self.state_store is not None:
                self.state_store.save_task(self.run_id, task)
            futures[task.id] = pool.submit(self._run_task, work)
            running.append(task)
            self._emit_lock_snapshot()

    def _finish_task(self, task_id: str, future: Future) -> None:
        task = self.graph.get(task_id)
        try:
            outcome = future.result()
        except Exception as exc:  # defensive: workers are wrapped
            outcome = {"status": "FAILED", "error": f"worker crashed: {exc}",
                       "attempts": task.attempts, "result": None}
        task.status = TaskStatus(outcome["status"])
        task.result = outcome.get("result")
        task.error = outcome.get("error", "")
        task.attempts = outcome.get("attempts", task.attempts)
        task.finished_at = time.time()
        self.locks.release(task_id)
        self._emit("lock_released", {"task_id": task.id,
                                     "at": task.finished_at})
        self._emit("task_finished", {
            "task_id": task.id, "role": task.role,
            "status": task.status.value, "attempts": task.attempts,
            "error": task.error, "at": task.finished_at})
        if self.state_store is not None:
            self.state_store.save_task(self.run_id, task)
        if task.status == TaskStatus.SUCCEEDED:
            self._handoff(task)
        self._emit_lock_snapshot()

    def _handoff(self, task: TaskNode) -> None:
        """Structured result hand-off to every direct dependent."""
        payload = json.dumps(task.result or {}, default=str)
        for dependent in self.graph.dependents(task.id):
            if dependent.status != TaskStatus.PENDING:
                continue
            self.message_bus.send(
                task.role, dependent.role, dependent.id, "task_result",
                content=payload,
                evidence=(f"from:{task.id}", f"role:{task.role}"),
                confidence=1.0)
            self._record_message()
            self._emit("message_sent", {
                "sender": task.role, "receiver": dependent.role,
                "task_id": dependent.id, "type": "task_result",
                "content": payload[:200], "at": time.time()})

    # -- one task ---------------------------------------------------------------

    def _run_task(self, work: TaskWorkItem) -> dict[str, Any]:
        task = work.task
        spec = get_role_spec(task.role)
        last_error = ""
        attempt = 0
        while True:
            attempt += 1
            task.attempts = attempt
            if attempt > 1:
                self._emit("task_retried", {
                    "task_id": task.id, "role": task.role, "attempt": attempt,
                    "previous_error": last_error, "at": time.time()})
            attempt_started = time.time()
            try:
                self._checkpoint()
                result = self._invoke_with_budget(work, spec)
                violations = validate_result(spec, result)
                if violations:
                    raise TaskError(
                        "result schema violation: " + "; ".join(violations))
                encoded = json.dumps(result, default=str).encode("utf-8")
                if len(encoded) > spec.limits.max_output_bytes:
                    raise TaskError(
                        f"result exceeds role limit "
                        f"{spec.limits.max_output_bytes} bytes "
                        f"({len(encoded)} delivered)")
                self._record_attempt(task, attempt, "SUCCEEDED", "",
                                     attempt_started)
                return {"status": "SUCCEEDED", "result": result,
                        "error": "", "attempts": attempt}
            except TaskCancelled:
                self._record_attempt(task, attempt, "CANCELLED",
                                     "cancelled by operator", attempt_started)
                return {"status": "CANCELLED", "result": None,
                        "error": "cancelled by operator",
                        "attempts": attempt}
            except TaskDeniedError as exc:
                self._record_attempt(task, attempt, "DENIED", str(exc),
                                     attempt_started)
                self._emit("task_denied", {
                    "task_id": task.id, "role": task.role, "reason": str(exc),
                    "at": time.time()})
                return {"status": "DENIED", "result": None,
                        "error": str(exc), "attempts": attempt}
            except TaskTimeoutError as exc:
                last_error = str(exc)
                self._record_attempt(task, attempt, "FAILED", last_error,
                                     attempt_started)
            except Exception as exc:
                last_error = str(exc) or exc.__class__.__name__
                self._record_attempt(task, attempt, "FAILED", last_error,
                                     attempt_started)
            if attempt > task.max_retries:
                return {"status": "FAILED", "result": None,
                        "error": last_error, "attempts": attempt}
            self._interruptible_sleep(self.retry_backoff * attempt)

    def _invoke_with_budget(self, work: TaskWorkItem,
                            spec: Any) -> dict[str, Any]:
        """Run the worker under its wall-clock budget.

        Cooperative workers call ``work.checkpoint()`` to observe
        cancellation; the budget is the hard backstop for non-cooperative
        workers (their abandoned thread keeps running as a daemon, and
        its late result is discarded — documented last-resort behavior).
        """
        task = work.task
        budget = task.timeout if task.timeout is not None else spec.timeout
        holder: dict[str, Any] = {}
        done = threading.Event()

        def target() -> None:
            worker = self.workers[task.role]
            try:
                holder["result"] = worker(work)
            except BaseException as exc:  # re-raised below
                holder["error"] = exc
            finally:
                done.set()

        thread = threading.Thread(target=target, daemon=True,
                                  name=f"forge-worker-{task.id}")
        thread.start()
        if not done.wait(timeout=budget):
            self._emit("task_timeout", {
                "task_id": task.id, "role": task.role, "budget": budget,
                "at": time.time()})
            raise TaskTimeoutError(
                f"Task {task.id} exceeded its {budget}s budget")
        if "error" in holder:
            raise holder["error"]
        return holder["result"]

    # -- helpers -------------------------------------------------------------------

    def _checkpoint(self) -> None:
        if self.control is not None:
            self.control.checkpoint("task")

    def _interruptible_sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.control is not None and self.control.cancel_requested:
                raise TaskCancelled("cancelled by operator")
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

    def _record_attempt(self, task: TaskNode, attempt: int, outcome: str,
                        error: str, started_at: float) -> None:
        if self.state_store is not None:
            self.state_store.record_attempt(
                self.run_id, task.id, attempt, outcome, error, started_at,
                time.time())

    def _record_message(self) -> None:
        """Journal the message just sent (callers invoke it right after
        ``message_bus.send``)."""
        if self.state_store is not None:
            log = self.message_bus.log()
            if log:
                self.state_store.record_message(self.run_id, log[-1].to_dict())

    def _emit_lock_snapshot(self) -> None:
        snapshot = self.locks.snapshot()
        self._emit("lock_snapshot", {"keys": snapshot["keys"]})

    def _emit(self, name: str, details: dict[str, Any]) -> None:
        payload = {"name": name, "at": time.time(), **details}
        self._events.append(payload)
        if self.state_store is not None:
            try:
                self.state_store.record_event(self.run_id, name, details)
            except Exception:
                pass
        try:
            self.activity.on_event(name, details)
        except Exception:
            pass
        if self.on_event is not None:
            try:
                self.on_event(name, dict(details))
            except Exception:
                pass
