"""Bounded model residency (Session 11).

One canonical loaded-model registry. It exists so that

* a model is **never loaded twice** because two agents asked for it
  (single-flight: the second caller waits for the first load and shares the
  entry),
* a model is **never unloaded while an active request references it**
  (reference counts),
* residency is **bounded** (byte budget, slot budget, deterministic LRU
  eviction, idle expiry),
* and every load/unload is recorded with timestamps so the governor, the
  router and telemetry all read the same truth.

Eviction is deterministic: idle entries first (oldest ``last_used_at``), then
least-recently-used, and only entries whose reference count is zero. An entry
that cannot be evicted because it is in use makes the load fail with
``RESIDENCY_FULL`` rather than silently exceeding the budget or stealing a
model out from under a running request.

Python floor: 3.8 (Windows 7 reference target). Stdlib only.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import (Any, Callable, Dict, Iterator, List, Optional, Tuple)

__all__ = [
    "ModelResidencyError",
    "ResidencyEntry",
    "ModelResidencyCache",
]


class ModelResidencyError(RuntimeError):
    """A residency operation was refused."""

    code = "RESIDENCY"

    def __init__(self, message: str, *, code: str = "RESIDENCY") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class ResidencyEntry:
    """One loaded model, its reference count and its timestamps."""

    model_id: str
    backend_id: str = ""
    size_bytes: int = 0
    fingerprint: str = ""
    loaded_at: float = 0.0
    last_used_at: float = 0.0
    refs: int = 0
    #: ``loaded`` | ``loading`` | ``unloading`` | ``evicted`` | ``failed``
    state: str = "loading"
    loads: int = 0
    evictions: int = 0
    pending_remove: bool = False
    error: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def in_use(self) -> bool:
        return self.refs > 0

    def idle_seconds(self, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        return max(0.0, now - float(self.last_used_at or 0.0))

    def to_dict(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "backend_id": self.backend_id,
            "size_bytes": int(self.size_bytes or 0),
            "fingerprint": self.fingerprint,
            "loaded_at": self.loaded_at,
            "last_used_at": self.last_used_at,
            "idle_seconds": round(self.idle_seconds(now), 3),
            "refs": int(self.refs),
            "state": self.state,
            "loads": int(self.loads),
            "evictions": int(self.evictions),
            "pending_remove": bool(self.pending_remove),
            "error": self.error[:300],
        }


class ModelResidencyCache:
    """The one canonical loaded-model registry."""

    def __init__(self, *, max_bytes: int = 0, max_slots: int = 1,
                 idle_seconds: float = 0.0,
                 unloader: Optional[Callable[[str, str], bool]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 load_timeout: float = 120.0) -> None:
        #: ``0`` means no explicit byte bound (the slot bound still applies).
        self.max_bytes = max(0, int(max_bytes or 0))
        self.max_slots = max(1, int(max_slots or 1))
        self.idle_seconds = max(0.0, float(idle_seconds or 0.0))
        self.load_timeout = max(0.5, float(load_timeout or 0.5))
        self._unloader = unloader
        self._clock = clock
        self._entries: Dict[str, ResidencyEntry] = {}
        self._events: Dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._resident = 0
        self._loads = 0
        self._duplicate_loads_avoided = 0
        self._evictions = 0
        self._unloads = 0
        #: Every residency event (eviction, explicit unload, refusal).
        self._events_log: List[Dict[str, Any]] = []
        #: Only genuine refusals: a request that was denied. An operator
        #: unload or a capacity eviction is bookkeeping, not a refusal, and
        #: mixing them would make the honest metrics lie in both directions.
        self._refusals: List[Dict[str, Any]] = []

    # -- introspection ---------------------------------------------------

    @property
    def resident_bytes(self) -> int:
        with self._lock:
            return int(self._resident)

    def get(self, model_id: str) -> Optional[ResidencyEntry]:
        with self._lock:
            return self._entries.get(model_id)

    def entries(self) -> List[ResidencyEntry]:
        with self._lock:
            return [self._entries[key] for key in sorted(self._entries)]

    def model_ids(self) -> List[str]:
        with self._lock:
            return sorted(self._entries)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "slots": len(self._entries),
                "max_slots": self.max_slots,
                "resident_bytes": int(self._resident),
                "max_bytes": int(self.max_bytes),
                "loads": int(self._loads),
                "duplicate_loads_avoided": int(self._duplicate_loads_avoided),
                "evictions": int(self._evictions),
                "unloads": int(self._unloads),
                "in_use": sum(1 for entry in self._entries.values()
                              if entry.refs > 0),
                "refusals": [dict(item) for item in self._refusals[-20:]],
                "events": [dict(item) for item in self._events_log[-20:]],
            }

    def snapshot(self) -> Dict[str, Any]:
        now = self._clock()
        with self._lock:
            return {
                "stats": self.stats(),
                "entries": [entry.to_dict(now=now)
                            for entry in self.entries()],
            }

    # -- acquire / release -----------------------------------------------

    def acquire(self, model_id: str,
                loader: Callable[[], Any], *,
                backend_id: str = "", size_bytes: int = 0,
                fingerprint: str = "",
                timeout: Optional[float] = None) -> ResidencyEntry:
        """Return a referenced entry for ``model_id``, loading it at most once.

        ``loader`` performs the real load (through the backend/runtime) and
        returns either an object with a ``size_bytes`` attribute, an ``int``
        size, or ``None``. It is called **only** when the model is not already
        resident and no other caller is loading it.
        """
        if not model_id:
            raise ModelResidencyError("model_id is required", code="BAD_REQUEST")
        bound = self.load_timeout if timeout is None else max(
            0.5, float(timeout))
        deadline = time.monotonic() + bound

        while True:
            with self._lock:
                entry = self._entries.get(model_id)
                if entry is not None and entry.state == "loaded":
                    entry.refs += 1
                    entry.last_used_at = self._clock()
                    if entry.pending_remove:
                        # A removal was deferred while it was in use; the new
                        # reference cancels it (an active request wins).
                        entry.pending_remove = False
                    return entry
                if entry is not None and entry.state == "loading":
                    # Single-flight: wait for the in-progress load instead of
                    # starting a second one.
                    wait_event = self._events.get(model_id)
                    self._duplicate_loads_avoided += 1
                else:
                    wait_event = None
                    if entry is not None and entry.state == "failed":
                        self._entries.pop(model_id, None)
                    self._entries[model_id] = ResidencyEntry(
                        model_id=model_id, backend_id=backend_id,
                        size_bytes=int(size_bytes or 0),
                        fingerprint=fingerprint, state="loading",
                        loaded_at=time.time(), last_used_at=self._clock())
                    self._events[model_id] = threading.Event()
                    wait_event = None

            if wait_event is not None:
                if not wait_event.wait(max(0.0, deadline - time.monotonic())):
                    raise ModelResidencyError(
                        "timed out waiting for the in-flight load of %s"
                        % model_id, code="LOAD_TIMEOUT")
                if time.monotonic() > deadline:
                    raise ModelResidencyError(
                        "residency wait exceeded its %.1fs bound for %s"
                        % (bound, model_id), code="LOAD_TIMEOUT")
                continue

            # This caller owns the load.
            event = self._events.get(model_id)
            try:
                self._make_room(model_id, int(size_bytes or 0))
                produced = loader()
                resolved = _resolve_size(produced, size_bytes)
                with self._lock:
                    entry = self._entries.get(model_id)
                    if entry is None:      # evicted/removed mid-load
                        entry = ResidencyEntry(model_id=model_id,
                                               backend_id=backend_id,
                                               loaded_at=time.time(),
                                               last_used_at=self._clock())
                        self._entries[model_id] = entry
                    entry.state = "loaded"
                    entry.size_bytes = int(resolved)
                    entry.backend_id = backend_id or entry.backend_id
                    entry.fingerprint = fingerprint or _resolve_fingerprint(
                        produced) or entry.fingerprint
                    entry.loads += 1
                    entry.refs += 1
                    entry.error = ""
                    entry.loaded_at = time.time()
                    entry.last_used_at = self._clock()
                    entry.metadata["loaded_handle"] = _describe(produced)
                    self._resident += int(resolved)
                    self._loads += 1
                    return entry
            except BaseException as exc:
                with self._lock:
                    entry = self._entries.get(model_id)
                    if entry is not None:
                        entry.state = "failed"
                        entry.error = str(exc)[:300]
                        entry.refs = 0
                    #: A residency refusal already recorded itself under its
                    #: own code (RESIDENCY_FULL / RESIDENCY_BUDGET). Logging it
                    #: again as LOAD_FAILED would double count one honest
                    #: refusal and blur the reason an operator sees.
                    already = (isinstance(exc, ModelResidencyError)
                               and getattr(exc, "code", "")
                               in _RESIDENCY_REFUSAL_CODES)
                    if not already:
                        self._record_refusal("LOAD_FAILED",
                                             "loading %s failed: %s"
                                             % (model_id, str(exc)[:200]))
                if isinstance(exc, ModelResidencyError):
                    raise
                raise ModelResidencyError(
                    "load of %s failed: %s" % (model_id, str(exc)[:300]),
                    code="LOAD_FAILED") from exc
            finally:
                if event is not None:
                    event.set()
                with self._lock:
                    self._events.pop(model_id, None)

    def release(self, model_id: str, *, count: int = 1) -> bool:
        """Drop references. Returns True while the entry stays resident."""
        with self._lock:
            entry = self._entries.get(model_id)
            if entry is None:
                return False
            entry.refs = max(0, entry.refs - max(1, int(count or 1)))
            entry.last_used_at = self._clock()
            if entry.refs == 0:
                if entry.pending_remove:
                    self._evict_entry(entry, reason="pending-remove")
                    return False
                return True
            return True

    @contextmanager
    def residency(self, model_id: str, loader: Callable[[], Any], *,
                  backend_id: str = "", size_bytes: int = 0,
                  fingerprint: str = "",
                  timeout: Optional[float] = None
                  ) -> Iterator[ResidencyEntry]:
        """Acquire for the duration of a block; always releases."""
        entry = self.acquire(model_id, loader, backend_id=backend_id,
                             size_bytes=size_bytes, fingerprint=fingerprint,
                             timeout=timeout)
        try:
            yield entry
        finally:
            self.release(model_id)

    def touch(self, model_id: str) -> None:
        with self._lock:
            entry = self._entries.get(model_id)
            if entry is not None:
                entry.last_used_at = self._clock()

    # -- removal / eviction ----------------------------------------------

    def remove(self, model_id: str) -> Dict[str, Any]:
        """Remove an entry safely.

        An entry with live references is **not** torn down: it is marked
        ``pending_remove`` and released when the last reference drops, so an
        active request is never invalidated by a registry removal.
        """
        with self._lock:
            entry = self._entries.get(model_id)
            if entry is None:
                return {"removed": False, "model_id": model_id,
                        "reason": "not resident"}
            if entry.refs > 0:
                entry.pending_remove = True
                self._record_event(
                    "REMOVE_DEFERRED",
                    "%s removal deferred: in use by %d reference(s)"
                    % (model_id, entry.refs))
                return {"removed": False, "deferred": True,
                        "model_id": model_id, "refs": entry.refs,
                        "reason": "in use by %d active reference(s); removal "
                                  "deferred until they are released"
                                  % entry.refs}
            self._evict_entry(entry, reason="removed")
            return {"removed": True, "model_id": model_id}

    def evict_idle(self, *, now: Optional[float] = None) -> List[str]:
        """Unload entries idle longer than ``idle_seconds`` (refs == 0 only)."""
        if self.idle_seconds <= 0.0:
            return []
        moment = self._clock() if now is None else now
        evicted: List[str] = []
        with self._lock:
            for entry in self.entries():
                if entry.refs > 0 or entry.state != "loaded":
                    continue
                if entry.idle_seconds(moment) >= self.idle_seconds:
                    evicted.append(entry.model_id)
                    self._evict_entry(entry, reason="idle")
        return evicted

    def evict_lru(self, needed_bytes: int = 0, *,
                  protect: Tuple[str, ...] = ()) -> List[str]:
        """Evict least-recently-used, unreferenced entries until room exists."""
        evicted: List[str] = []
        with self._lock:
            candidates = [entry for entry in self._entries.values()
                          if entry.refs == 0 and entry.state == "loaded"
                          and entry.model_id not in protect]
            candidates.sort(key=lambda entry: (entry.last_used_at,
                                               entry.model_id))
            for entry in candidates:
                if self._has_room(needed_bytes, extra_slots=1,
                                  ignore=entry.model_id):
                    break
                evicted.append(entry.model_id)
                self._evict_entry(entry, reason="lru")
        return evicted

    def _make_room(self, model_id: str, size_bytes: int) -> None:
        """Expire idle entries, then LRU-evict, then refuse honestly."""
        self.evict_idle()
        with self._lock:
            if self._has_room(size_bytes, extra_slots=1, ignore=model_id):
                return
            in_use = [entry.model_id for entry in self._entries.values()
                      if entry.refs > 0 and entry.model_id != model_id]
            slots = len([key for key in self._entries if key != model_id])
        evicted = self.evict_lru(size_bytes, protect=(model_id,))
        with self._lock:
            if self._has_room(size_bytes, extra_slots=1, ignore=model_id):
                return
            if len(self._entries) - (1 if model_id in self._entries else 0) \
                    >= self.max_slots:
                reason = ("residency is full: %d/%d slots in use%s"
                          % (slots, self.max_slots,
                             (" by " + ", ".join(sorted(in_use)))
                             if in_use else ""))
                code = "RESIDENCY_FULL"
            else:
                reason = ("model of %d bytes does not fit the %d byte "
                          "residency budget (%d held%s)"
                          % (size_bytes, self.max_bytes, self._resident,
                             ", in use: " + ", ".join(sorted(in_use))
                             if in_use else ""))
                code = "RESIDENCY_BUDGET"
            self._record_refusal(code, reason)
        raise ModelResidencyError(
            reason + (" (evicted %s)" % ", ".join(evicted) if evicted else ""),
            code=code)

    def _has_room(self, size_bytes: int, *, extra_slots: int = 0,
                  ignore: str = "") -> bool:
        with self._lock:
            slots = len([key for key in self._entries
                         if key != ignore
                         and self._entries[key].state != "failed"])
            if slots + extra_slots > self.max_slots:
                return False
            if self.max_bytes <= 0:
                return True
            held = sum(entry.size_bytes for key, entry in self._entries.items()
                       if key != ignore and entry.state == "loaded")
            return held + int(size_bytes or 0) <= self.max_bytes

    def _evict_entry(self, entry: ResidencyEntry, *, reason: str) -> None:
        """Tear down one entry (caller holds the lock).

        ``reason`` decides how this is counted, because the three cases mean
        different things to an operator reading the metrics:

        * ``removed``/``clear``/``unloaded`` — an explicit unload. Not an
          eviction, and never a refusal.
        * ``idle``/``lru`` — real eviction under residency pressure.
        """
        explicit = reason in _EXPLICIT_UNLOAD_REASONS
        if self._unloader is not None:
            try:
                self._unloader(entry.model_id, entry.backend_id)
            except Exception as exc:
                entry.error = "unload failed: %s" % str(exc)[:200]
                self._record_event("UNLOAD_FAILED",
                                   "%s unload failed: %s"
                                   % (entry.model_id, str(exc)[:200]))
        self._resident = max(0, self._resident - int(entry.size_bytes or 0))
        entry.state = "unloaded" if explicit else "evicted"
        entry.refs = 0
        entry.pending_remove = False
        entry.metadata.pop("loaded_handle", None)
        self._entries.pop(entry.model_id, None)
        if explicit:
            self._unloads += 1
            self._record_event("UNLOADED", "%s unloaded (%s)"
                               % (entry.model_id, reason))
        else:
            entry.evictions += 1
            self._evictions += 1
            self._record_event("EVICTED", "%s evicted (%s)"
                               % (entry.model_id, reason))

    def clear(self) -> List[str]:
        """Unload every unreferenced entry. In-use entries are deferred."""
        removed: List[str] = []
        with self._lock:
            for entry in self.entries():
                if entry.refs > 0:
                    entry.pending_remove = True
                    continue
                removed.append(entry.model_id)
                self._evict_entry(entry, reason="clear")
        return removed

    def _record_event(self, code: str, message: str) -> None:
        """Record one residency event; refusals are indexed separately."""
        record = {"code": code, "message": message[:300], "at": time.time()}
        self._events_log.append(record)
        if len(self._events_log) > 100:
            self._events_log = self._events_log[-100:]
        if code in _REFUSAL_CODES:
            self._refusals.append(dict(record))
            if len(self._refusals) > 100:
                self._refusals = self._refusals[-100:]

    def _record_refusal(self, code: str, message: str) -> None:
        self._record_event(code, message)


#: Reasons that mean "an operator asked for this unload" (not eviction).
_EXPLICIT_UNLOAD_REASONS = frozenset({"removed", "clear", "unloaded",
                                      "shutdown"})

#: Event codes that represent a denied request (surfaced as ``refusals``).
#: Refusals that come from residency pressure rather than from a failed load.
_RESIDENCY_REFUSAL_CODES = frozenset({"RESIDENCY_FULL", "RESIDENCY_BUDGET"})

_REFUSAL_CODES = frozenset({"RESIDENCY_FULL", "RESIDENCY_BUDGET",
                            "LOAD_FAILED", "IN_USE", "RESIDENCY_DENIED"})


def _resolve_size(produced: Any, fallback: int) -> int:
    if isinstance(produced, bool):
        return int(fallback or 0)
    if isinstance(produced, int):
        return max(0, int(produced))
    for attribute in ("size_bytes", "resident_bytes"):
        value = getattr(produced, attribute, None)
        if isinstance(value, int) and value >= 0:
            return int(value)
    metadata = getattr(produced, "metadata", None)
    if isinstance(metadata, dict):
        for key in ("resident_bytes", "size_bytes"):
            value = metadata.get(key)
            if isinstance(value, int) and value >= 0:
                return int(value)
    return int(fallback or 0)


def _resolve_fingerprint(produced: Any) -> str:
    metadata = getattr(produced, "metadata", None)
    if isinstance(metadata, dict):
        value = metadata.get("fingerprint")
        if isinstance(value, str):
            return value
    return ""


def _describe(produced: Any) -> str:
    """A bounded, content-free description of a load handle."""
    if produced is None:
        return "none"
    if isinstance(produced, (int, bool, str)):
        return str(produced)[:80]
    return type(produced).__name__
