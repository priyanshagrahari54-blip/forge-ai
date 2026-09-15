"""Bounded streaming for the inference fabric (Session 11).

A stream is a sequence of :class:`StreamEvent` values carrying

``request_id`` · ``sequence`` · ``timestamp`` · ``model_id`` · ``delta`` ·
``done`` · ``error``

Guarantees, all enforced here rather than documented:

* **Monotonic sequence** — assigned by the stream, never by the producer, and
  gaps are impossible (a producer that stalls produces no event at all).
* **Bounded buffer** — a fixed-capacity queue. There is no code path that
  accumulates the whole completion in memory: only ``max_total_chars`` of
  text is ever retained, and only for the caller's convenience.
* **Backpressure** — publishing into a full buffer blocks up to
  ``put_timeout``; when the consumer is still not draining, the stream fails
  with ``STREAM_OVERFLOW`` instead of growing.
* **Disconnect handling** — :meth:`BoundedStream.close` ends iteration and
  makes further publishing a no-op, so a vanished consumer cannot wedge a
  producer thread.
* **Cancellation** — :meth:`BoundedStream.cancel` terminates the stream with
  ``done=True`` and ``error_code="cancelled"``.
* **Honesty** — ``done`` marks the end of the stream; ``complete`` is a
  separate question. A cancelled, failed, or truncated stream is ``done`` but
  never ``complete``.

Python floor: 3.8 (Windows 7 reference target). Stdlib only.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

__all__ = [
    "StreamEvent",
    "BoundedStream",
    "StreamState",
    "join_deltas",
]

#: Default buffer capacity (events), and the default total-character bound.
DEFAULT_MAX_BUFFER = 64
DEFAULT_MAX_TOTAL_CHARS = 64 * 1024
DEFAULT_PUT_TIMEOUT = 5.0


class StreamState:
    OPEN = "open"
    COMPLETED = "completed"
    TRUNCATED = "truncated"
    CANCELLED = "cancelled"
    FAILED = "failed"
    OVERFLOW = "overflow"
    DISCONNECTED = "disconnected"


_TERMINAL = object()


@dataclass
class StreamEvent:
    """One streamed piece of output."""

    request_id: str = ""
    sequence: int = 0
    timestamp: float = field(default_factory=time.time)
    model_id: str = ""
    backend_id: str = ""
    delta: str = ""
    done: bool = False
    error: str = ""
    error_code: str = ""
    finish_reason: str = ""
    output_tokens: int = 0
    state: str = StreamState.OPEN
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_text: bool = False) -> Dict[str, Any]:
        """A loggable view. Text is excluded unless explicitly requested."""
        payload: Dict[str, Any] = {
            "request_id": self.request_id,
            "sequence": int(self.sequence),
            "timestamp": self.timestamp,
            "model_id": self.model_id,
            "backend_id": self.backend_id,
            "delta_chars": len(self.delta or ""),
            "done": bool(self.done),
            "error": self.error[:400],
            "error_code": self.error_code,
            "finish_reason": self.finish_reason,
            "output_tokens": int(self.output_tokens or 0),
            "state": self.state,
        }
        if include_text:
            payload["delta"] = self.delta
        return payload

    def to_wire(self) -> Dict[str, Any]:
        """Transport form for a thin client (includes the delta text only)."""
        return {
            "request_id": self.request_id,
            "sequence": int(self.sequence),
            "timestamp": self.timestamp,
            "model_id": self.model_id,
            "backend_id": self.backend_id,
            "delta": self.delta,
            "done": bool(self.done),
            "error": self.error[:400],
            "error_code": self.error_code,
            "finish_reason": self.finish_reason,
            "output_tokens": int(self.output_tokens or 0),
            "state": self.state,
        }


class BoundedStream:
    """A bounded, cancellable, backpressured event stream."""

    def __init__(self, request_id: str, *, model_id: str = "",
                 backend_id: str = "", max_buffer: int = DEFAULT_MAX_BUFFER,
                 max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
                 put_timeout: float = DEFAULT_PUT_TIMEOUT,
                 get_timeout: Optional[float] = None,
                 retain_text: bool = True) -> None:
        if not request_id:
            raise ValueError("request_id is required")
        self.request_id = request_id
        self.model_id = model_id
        self.backend_id = backend_id
        self.max_buffer = max(1, int(max_buffer or 1))
        self.max_total_chars = max(0, int(max_total_chars or 0))
        self.put_timeout = max(0.05, float(put_timeout or 0.05))
        #: Overall consumption bound; ``None`` = no wall-clock bound here (the
        #: runtime's own timeout still applies to the generation).
        self.get_timeout = get_timeout
        self.retain_text = bool(retain_text)
        self._queue: "queue.Queue" = queue.Queue(maxsize=self.max_buffer)
        self._lock = threading.RLock()
        self._sequence = 0
        self._chars = 0
        self._truncated = False
        self._closed = False
        self._cancelled = False
        self._cancel_reason = ""
        self._state = StreamState.OPEN
        self._error = ""
        self._error_code = ""
        self._finish_reason = ""
        self._dropped = 0
        self._overflow = 0
        self._started = time.monotonic()
        self._first_event_at: Optional[float] = None
        self._ended_at: Optional[float] = None
        self._text: List[str] = [] if retain_text else []

    # -- producer side ---------------------------------------------------

    def publish(self, delta: str, *, output_tokens: int = 0,
                metadata: Optional[Dict[str, Any]] = None) -> bool:
        """Publish one delta. ``False`` when the stream will not accept more."""
        if not delta:
            return not self._closed
        with self._lock:
            if self._closed or self._cancelled:
                self._dropped += 1
                return False
            if self.max_total_chars and \
                    self._chars + len(delta) > self.max_total_chars:
                room = self.max_total_chars - self._chars
                if room <= 0:
                    self._truncated = True
                    self._dropped += len(delta)
                    return False
                delta = delta[:room]
                self._truncated = True
            self._chars += len(delta)
            if self.retain_text:
                self._text.append(delta)
            self._sequence += 1
            event = StreamEvent(
                request_id=self.request_id, sequence=self._sequence,
                model_id=self.model_id, backend_id=self.backend_id,
                delta=delta, output_tokens=int(output_tokens or 0),
                state=self._state, metadata=dict(metadata or {}))
        if self._first_event_at is None:
            self._first_event_at = time.monotonic()
        try:
            self._queue.put(event, timeout=self.put_timeout)
            return True
        except queue.Full:
            with self._lock:
                self._overflow += 1
            self.fail("consumer stopped draining the stream buffer",
                      code="STREAM_OVERFLOW",
                      finish_reason="error")
            return False

    def finish(self, *, finish_reason: str = "stop", output_tokens: int = 0,
               metadata: Optional[Dict[str, Any]] = None) -> StreamEvent:
        """End the stream successfully (``done=True``)."""
        with self._lock:
            if self._state != StreamState.OPEN:
                return self._terminal_locked()
            self._state = (StreamState.TRUNCATED if self._truncated
                           else StreamState.COMPLETED)
            self._finish_reason = (
                "length" if self._truncated else (finish_reason or "stop"))
        return self._emit_terminal(output_tokens=output_tokens,
                                   metadata=metadata)

    def fail(self, error: str, *, code: str = "STREAM_FAILED",
             finish_reason: str = "error",
             output_tokens: int = 0) -> StreamEvent:
        """End the stream with an error. The failure is never hidden."""
        with self._lock:
            if self._state != StreamState.OPEN:
                return self._terminal_locked()
            self._state = (StreamState.OVERFLOW if code == "STREAM_OVERFLOW"
                           else StreamState.FAILED)
            self._error = str(error or "")[:400]
            self._error_code = code
            self._finish_reason = finish_reason or "error"
        return self._emit_terminal(output_tokens=output_tokens)

    def cancel(self, reason: str = "cancelled") -> bool:
        """Cancel the stream; a terminal event is always delivered."""
        with self._lock:
            if self._state != StreamState.OPEN:
                return False
            self._cancelled = True
            self._cancel_reason = str(reason or "cancelled")[:200]
            self._state = StreamState.CANCELLED
            self._error = "stream cancelled (%s)" % self._cancel_reason
            self._error_code = "cancelled"
            self._finish_reason = "cancelled"
        self._emit_terminal()
        return True

    def close(self) -> None:
        """Consumer disconnected: stop accepting and end iteration."""
        with self._lock:
            self._closed = True
            if self._state == StreamState.OPEN:
                self._state = StreamState.DISCONNECTED
                self._finish_reason = "disconnected"
        self._enqueue_terminal_marker(_TERMINAL)

    def _enqueue_terminal_marker(self, item: Any) -> bool:
        """Queue a terminal item, making room for it if the buffer is full.

        A terminal marker outranks a buffered delta no consumer has taken yet.
        Without this an overflowed stream would strand its consumer waiting
        for an end that could never be queued. Anything displaced is counted
        in ``dropped_chars``, so the loss is reported rather than silent.
        """
        #: The wait is short on purpose: a terminal event must not inherit the
        #: producer's backpressure budget, or cancellation looks like a hang.
        try:
            self._queue.put(item, timeout=min(self.put_timeout, 0.25))
            return True
        except queue.Full:
            pass
        try:
            stale = self._queue.get_nowait()
        except queue.Empty:
            stale = None
        if isinstance(stale, StreamEvent):
            with self._lock:
                self._dropped += len(stale.delta or "")
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            #: Nothing more can be done; the stream object itself still
            #: reports the honest terminal state through ``snapshot()``.
            return False

    def _emit_terminal(self, *, output_tokens: int = 0,
                       metadata: Optional[Dict[str, Any]] = None
                       ) -> StreamEvent:
        with self._lock:
            event = self._terminal_locked(output_tokens=output_tokens,
                                          metadata=metadata)
            self._ended_at = time.monotonic()
        self._enqueue_terminal_marker(event)
        self._enqueue_terminal_marker(_TERMINAL)
        return event

    def _terminal_locked(self, *, output_tokens: int = 0,
                         metadata: Optional[Dict[str, Any]] = None
                         ) -> StreamEvent:
        self._sequence += 1
        return StreamEvent(
            request_id=self.request_id, sequence=self._sequence,
            model_id=self.model_id, backend_id=self.backend_id, delta="",
            done=True, error=self._error, error_code=self._error_code,
            finish_reason=self._finish_reason,
            output_tokens=int(output_tokens or 0), state=self._state,
            metadata=dict(metadata or {}))

    # -- consumer side ---------------------------------------------------

    def __iter__(self) -> Iterator[StreamEvent]:
        return self.events()

    def events(self) -> Iterator[StreamEvent]:
        """Yield events until the terminal one. Never yields after ``done``."""
        deadline = (time.monotonic() + float(self.get_timeout)
                    if self.get_timeout else None)
        saw_terminal = False
        while True:
            timeout = None
            if deadline is not None:
                timeout = max(0.0, deadline - time.monotonic())
            try:
                item = self._queue.get(timeout=timeout) \
                    if timeout is not None else self._queue.get()
            except queue.Empty:
                self.fail("stream consumption exceeded its %.1fs bound"
                          % float(self.get_timeout or 0.0),
                          code="STREAM_TIMEOUT")
                with self._lock:
                    yield self._terminal_locked()
                return
            if item is _TERMINAL:
                if not saw_terminal:
                    # A producer that vanished without a terminal event still
                    # gets one, so the consumer is never left hanging.
                    self.fail("producer ended without a terminal event",
                              code="STREAM_TRUNCATED")
                    with self._lock:
                        yield self._terminal_locked()
                return
            if isinstance(item, StreamEvent):
                saw_terminal = bool(item.done)
                yield item
                if item.done:
                    # Drain the sentinel so a later iteration cannot re-emit.
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        pass
                    return

    def text(self) -> str:
        """The retained deltas (bounded by ``max_total_chars``)."""
        with self._lock:
            return "".join(self._text)

    # -- state -----------------------------------------------------------

    @property
    def done(self) -> bool:
        with self._lock:
            return self._state != StreamState.OPEN

    @property
    def complete(self) -> bool:
        """True only for a stream that finished without error or truncation."""
        with self._lock:
            return (self._state == StreamState.COMPLETED
                    and not self._error and not self._truncated)

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    @property
    def chars(self) -> int:
        with self._lock:
            return self._chars

    def snapshot(self) -> Dict[str, Any]:
        """Metadata only — never the streamed text."""
        with self._lock:
            elapsed = ((self._ended_at or time.monotonic()) - self._started)
            first = (self._first_event_at - self._started
                     if self._first_event_at else None)
            return {
                "request_id": self.request_id,
                "model_id": self.model_id,
                "backend_id": self.backend_id,
                "state": self._state,
                "done": self._state != StreamState.OPEN,
                "complete": (self._state == StreamState.COMPLETED
                             and not self._error and not self._truncated),
                "sequence": self._sequence,
                "chars": self._chars,
                "truncated": self._truncated,
                "dropped_chars": self._dropped,
                "overflow_events": self._overflow,
                "cancelled": self._cancelled,
                "cancel_reason": self._cancel_reason,
                "error": self._error[:400],
                "error_code": self._error_code,
                "finish_reason": self._finish_reason,
                "buffer_capacity": self.max_buffer,
                "buffered": self._queue.qsize(),
                "max_total_chars": self.max_total_chars,
                "elapsed_ms": round(elapsed * 1000.0, 3),
                "time_to_first_token_ms": (round(first * 1000.0, 3)
                                           if first is not None else None),
            }


def join_deltas(events: Any) -> Dict[str, Any]:
    """Consume an event iterable and report what actually arrived.

    Returns ``text`` plus honesty flags: a stream that never reported
    ``done=True`` is reported as partial, whatever text arrived.
    """
    parts: List[str] = []
    done = False
    error = ""
    error_code = ""
    finish_reason = ""
    sequences: List[int] = []
    for event in events:
        sequences.append(int(getattr(event, "sequence", 0) or 0))
        delta = getattr(event, "delta", "")
        if delta:
            parts.append(delta)
        if getattr(event, "done", False):
            done = True
            error = getattr(event, "error", "") or error
            error_code = getattr(event, "error_code", "") or error_code
            finish_reason = (getattr(event, "finish_reason", "")
                             or finish_reason)
    monotonic = all(b > a for a, b in zip(sequences, sequences[1:]))
    return {
        "text": "".join(parts),
        "chars": sum(len(part) for part in parts),
        "done": bool(done),
        "complete": bool(done and not error and finish_reason
                         not in ("cancelled", "error", "disconnected")),
        "partial": not done,
        "error": error,
        "error_code": error_code,
        "finish_reason": finish_reason,
        "events": len(sequences),
        "monotonic_sequence": monotonic,
    }
