"""Execution fencing for task attempts (Session 10, Section 3).

A timed-out or cancelled worker thread cannot be killed reliably in pure
Python, so safety must come from *authorization*, not from thread
lifecycle: every execution attempt carries a unique identity, and every
state-changing operation re-checks that identity before it commits.

The identity is a ``(task_id, generation)`` pair. The generation is a
monotonic counter per task kept by :class:`FenceRegistry`; a new attempt
always starts at a strictly higher generation than any abandoned
previous one, so retries can never overlap an abandoned attempt, and a
stale attempt — one whose worker is still running after the scheduler
timed it out or cancelled it — can never commit state or write files:

* :meth:`FenceRegistry.commit` accepts a terminal state only from the
  currently-authorized generation (compare-and-swap). A stale attempt
  raises :class:`StaleAttemptError` instead of clobbering the fresh
  state.
* :meth:`FenceRegistry.fence` invalidates an attempt (timeout,
  cancellation, crash, restart). Once fenced, the attempt's
  :func:`commit_guard` refuses every further write, and its
  :meth:`AttemptFence.authorized` reports False.
* The state machine is explicit and one-way at its terminals::

      QUEUED → RUNNING → SUCCEEDED
                        → FAILED
                        → CANCELLING → CANCELLED
                        → TIMED_OUT → FENCED

  Terminal states are immutable: a fenced or completed task cannot be
  moved again, and a restart cannot resurrect them (persistence is the
  scheduler's job; this module only defines the transitions).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

__all__ = [
    "AttemptFence",
    "FenceRegistry",
    "StaleAttemptError",
    "StateConflictError",
    "FENCED",
    "QUEUED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLING",
    "CANCELLED",
    "TIMED_OUT",
    "TERMINAL_STATES",
    "commit_guard",
]

# -- states ------------------------------------------------------------------

QUEUED = "QUEUED"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELLING = "CANCELLING"
CANCELLED = "CANCELLED"
TIMED_OUT = "TIMED_OUT"
FENCED = "FENCED"
#: Task-level terminal: never started because a dependency ended without
#: success. No attempt is ever created for a skipped task.
SKIPPED = "SKIPPED"

#: States nothing may leave. Terminal states persist: a restart reads
#: them back and refuses to move them again.
TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED, FENCED})

#: The legal transitions. Anything not listed is a conflict.
_TRANSITIONS = {
    QUEUED: (RUNNING, CANCELLING, TIMED_OUT, FENCED, FAILED),
    RUNNING: (SUCCEEDED, FAILED, CANCELLING, TIMED_OUT, FENCED),
    CANCELLING: (CANCELLED, FENCED),
    TIMED_OUT: (FENCED),
}

#: States in which an attempt is still authorized to commit work.
AUTHORIZED_STATES = frozenset({QUEUED, RUNNING})


class StaleAttemptError(RuntimeError):
    """A fenced or superseded attempt tried to commit state.

    Raised (or returned as a reason) whenever the committing generation
    is not the registry's currently-authorized generation for the task.
    """


class StateConflictError(RuntimeError):
    """A state transition that the machine does not allow."""


@dataclass(frozen=True)
class AttemptFence:
    """Identity of one execution attempt of one task."""

    task_id: str
    attempt: int
    generation: int
    state: str = QUEUED
    owner: str = ""
    created_at: float = field(default_factory=time.time)
    reason: str = ""

    @property
    def attempt_id(self) -> str:
        """Stable, human-readable attempt identity (task # generation)."""
        return "%s#g%d" % (self.task_id, self.generation)

    def authorized(self) -> bool:
        """True while this attempt may still commit results and writes."""
        return self.state in AUTHORIZED_STATES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "attempt": self.attempt,
            "generation": self.generation,
            "attempt_id": self.attempt_id,
            "state": self.state,
            "owner": self.owner,
            "created_at": self.created_at,
            "reason": self.reason,
        }


class FenceRegistry:
    """Thread-safe authority on which attempt may commit for each task.

    The registry is deliberately tiny and dependency-free so the same
    object can back the in-process DAG scheduler, the A38 orchestrator,
    and any worker that needs to prove it is still allowed to write.

    All methods are safe to call from any thread; the internal state is
    guarded by a reentrant-free lock and every mutation is atomic under
    it, so two racing attempts can never both observe themselves as
    authorized.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generations: Dict[str, int] = {}
        self._fences: Dict[Tuple[str, int], AttemptFence] = {}
        self._current: Dict[str, int] = {}

    # -- lifecycle -----------------------------------------------------------

    def begin(self, task_id: str, *, owner: str = "") -> AttemptFence:
        """Start a new attempt for ``task_id`` and fence its predecessor.

        The previous generation (if any) is fenced immediately with the
        reason ``superseded`` — an abandoned attempt stops being
        authorized the instant its retry starts, so a stale worker can
        never race a fresh one into shared state.
        """
        if not task_id:
            raise ValueError("task_id is required")
        with self._lock:
            generation = self._generations.get(task_id, 0) + 1
            self._generations[task_id] = generation
            previous = self._current.get(task_id)
            if previous is not None:
                old = self._fences.get((task_id, previous))
                if old is not None and old.state in AUTHORIZED_STATES:
                    self._fences[task_id, previous] = _fenced(
                        old, "superseded by generation %d" % generation)
            attempt_no = generation
            fence = AttemptFence(
                task_id=task_id, attempt=attempt_no, generation=generation,
                state=QUEUED, owner=owner or "")
            self._fences[(task_id, generation)] = fence
            self._current[task_id] = generation
            return fence

    def mark_running(self, task_id: str, fence: AttemptFence) -> AttemptFence:
        return self._transition(task_id, fence, RUNNING)

    def transition(self, task_id: str, fence: AttemptFence,
                   to_state: str, *, reason: str = "") -> AttemptFence:
        if to_state not in (SUCCEEDED, FAILED, CANCELLED, FENCED,
                            CANCELLING, TIMED_OUT, RUNNING):
            raise StateConflictError("unknown state %r" % (to_state,))
        return self._transition(task_id, fence, to_state, reason=reason)

    def fence(self, task_id: str, fence: AttemptFence,
              *, reason: str = "") -> AttemptFence:
        """Invalidate the attempt; every later commit or write is refused."""
        if not reason:
            reason = "fenced"
        if fence.state in (TIMED_OUT, CANCELLING):
            # Two-phase terminations: RUNNING → TIMED_OUT → FENCED and
            # RUNNING → CANCELLING → CANCELLED (or → FENCED when the
            # worker never confirms).
            return self._transition(task_id, fence, FENCED, reason=reason)
        if fence.state in TERMINAL_STATES:
            return fence
        if fence.state == CANCELLED:
            return fence
        # From QUEUED/RUNNING straight to FENCED (crash, restart, abort).
        return self._transition(task_id, fence, FENCED, reason=reason)

    def cancel(self, task_id: str) -> Optional[AttemptFence]:
        """Ask the current attempt to stop: RUNNING → CANCELLING.

        Returns the fence moved to CANCELLING, or ``None`` when the task
        has no cancellable attempt (unknown, already terminal, or
        already CANCELLING). CANCELLING is not yet terminal: the worker
        confirms the stop (→ CANCELLED) or is abandoned (→ FENCED).
        """
        with self._lock:
            generation = self._current.get(task_id)
            if generation is None:
                return None
            fence = self._fences.get((task_id, generation))
            if fence is None or fence.state != RUNNING:
                # Unknown, already CANCELLING, or terminal: nothing to move.
                return fence
        return self._transition(task_id, fence, CANCELLING,
                                reason="cancel requested")

    def confirm_cancelled(self, task_id: str, fence: AttemptFence,
                          *, reason: str = "") -> AttemptFence:
        return self._transition(task_id, fence, CANCELLED,
                                reason=reason or "cancelled")

    def timeout(self, task_id: str, fence: AttemptFence,
                *, reason: str = "") -> AttemptFence:
        """RUNNING → TIMED_OUT: the worker may still be alive; it is no
        longer authorized. The scheduler follows with :meth:`fence`."""
        return self._transition(
            task_id, fence, TIMED_OUT,
            reason=reason or "attempt timed out")

    def commit(self, task_id: str, fence: AttemptFence, terminal: str,
               *, payload: Any = None) -> Dict[str, Any]:
        """Record a terminal state — only for the authorized generation.

        Returns the durable record. Raises :class:`StaleAttemptError`
        when the fence is not the current generation, is already
        fenced, or is in a state that cannot reach ``terminal``. The
        payload is recorded verbatim; the caller is responsible for
        keeping it bounded.
        """
        if terminal not in (SUCCEEDED, FAILED, CANCELLED):
            raise StateConflictError("not a terminal state: %r" % (terminal,))
        with self._lock:
            generation = self._current.get(task_id)
            if generation != fence.generation:
                raise StaleAttemptError(
                    "attempt %s is stale: task %s is now generation %s"
                    % (fence.attempt_id, task_id,
                       generation if generation is not None else "unknown"))
            current = self._fences.get((task_id, fence.generation))
            if current is None:
                raise StaleAttemptError(
                    "attempt %s is no longer authorized to commit"
                    % (fence.attempt_id,))
            if current.state in TERMINAL_STATES or current.state == CANCELLED:
                raise StaleAttemptError(
                    "attempt %s already reached %s"
                    % (fence.attempt_id, current.state))
            # Which terminal states each live state may commit.
            if terminal == SUCCEEDED and current.state not in (
                    QUEUED, RUNNING):
                raise StaleAttemptError(
                    "attempt %s cannot succeed from state %s"
                    % (fence.attempt_id, current.state))
            if terminal == CANCELLED and current.state not in (
                    QUEUED, RUNNING, CANCELLING, TIMED_OUT):
                raise StaleAttemptError(
                    "attempt %s cannot be cancelled from state %s"
                    % (fence.attempt_id, current.state))
            # FAILED is reachable from any non-terminal state.
            self._fences[(task_id, fence.generation)] = AttemptFence(
                task_id=task_id, attempt=fence.attempt,
                generation=fence.generation, state=terminal,
                owner=fence.owner, created_at=fence.created_at,
                reason="committed %s" % terminal)
        return {"task_id": task_id, "attempt_id": fence.attempt_id,
                "generation": fence.generation, "state": terminal,
                "payload": payload,
                "committed_at": time.time()}

    # -- queries ---------------------------------------------------------------

    def current(self, task_id: str) -> Optional[AttemptFence]:
        with self._lock:
            generation = self._current.get(task_id)
            if generation is None:
                return None
            return self._fences.get((task_id, generation))

    def get(self, task_id: str, generation: int) -> Optional[AttemptFence]:
        with self._lock:
            return self._fences.get((task_id, generation))

    def generation_of(self, task_id: str) -> int:
        with self._lock:
            return self._generations.get(task_id, 0)

    def seed_generation(self, task_id: str, generation: int) -> None:
        """Restore a persisted high-water mark (restart recovery).

        A fresh process must never restart at generation 1: the old
        process's fenced attempts already own those generations, and
        reusing one would let a stale attempt look current. The seed
        only ever raises the counter, never lowers it.
        """
        if generation < 1:
            return
        with self._lock:
            if generation > self._generations.get(task_id, 0):
                self._generations[task_id] = int(generation)

    def is_authorized(self, fence: AttemptFence) -> bool:
        """False once the fence is stale or in a non-authorized state.

        The registry's view of the fence wins over the (possibly stale)
        copy the caller holds: attempts are frozen records, and the
        registry is the authority on their current state.
        """
        if fence.state not in AUTHORIZED_STATES:
            return False
        with self._lock:
            if self._current.get(fence.task_id) != fence.generation:
                return False
            current = self._fences.get((fence.task_id, fence.generation))
            if current is None:
                return False
            return current.state in AUTHORIZED_STATES

    # -- internals ---------------------------------------------------------------

    def _transition(self, task_id: str, fence: AttemptFence, to_state: str,
                    *, reason: str = "") -> AttemptFence:
        with self._lock:
            generation = self._current.get(task_id)
            if generation != fence.generation:
                raise StaleAttemptError(
                    "attempt %s is stale: task %s is now generation %s"
                    % (fence.attempt_id, task_id,
                       generation if generation is not None else "unknown"))
            current = self._fences.get((task_id, fence.generation))
            if current is None:
                raise StateConflictError(
                    "no attempt %s registered" % (fence.attempt_id,))
            if current.state in TERMINAL_STATES:
                raise StateConflictError(
                    "attempt %s is terminal (%s); cannot move to %s"
                    % (fence.attempt_id, current.state, to_state))
            if current.state == CANCELLED:
                raise StateConflictError(
                    "attempt %s is cancelled; cannot move to %s"
                    % (fence.attempt_id, to_state))
            allowed = _TRANSITIONS.get(current.state, ())
            if to_state not in allowed:
                raise StateConflictError(
                    "attempt %s cannot move %s -> %s"
                    % (fence.attempt_id, current.state, to_state))
            updated = AttemptFence(
                task_id=task_id, attempt=fence.attempt,
                generation=fence.generation, state=to_state,
                owner=fence.owner, created_at=fence.created_at,
                reason=reason)
            self._fences[(task_id, fence.generation)] = updated
            return updated


def _fenced(fence: AttemptFence, reason: str) -> AttemptFence:
    return AttemptFence(
        task_id=fence.task_id, attempt=fence.attempt,
        generation=fence.generation, state=FENCED, owner=fence.owner,
        created_at=fence.created_at, reason=reason)


def commit_guard(fence: AttemptFence,
                 registry: Optional[FenceRegistry] = None) -> Callable[[], str]:
    """Build the guard callable handed to the write choke points.

    The guard returns ``""`` while the attempt is authorized and a
    short reason string once it is fenced, stale, or terminal. Choke
    points (:class:`ChangeApplier`, :class:`ToolRuntime`, the mediated
    agent runtime) treat a non-empty return as a hard refusal: the
    write never happens, and the failure names the fence.
    """

    def _guard() -> str:
        if not fence.authorized():
            return ("attempt %s is fenced (%s)"
                    % (fence.attempt_id, fence.reason or fence.state))
        if registry is not None and not registry.is_authorized(fence):
            return "attempt %s is no longer the authorized generation" \
                % (fence.attempt_id,)
        return ""

    return _guard
