"""Provider quota/rate-limit exhaustion tracking.

"One provider's limit is spent, use the next one" needs two things Forge did
not have: a way to *recognise* an exhausted provider, and a memory of it. The
failover chain alone retried the same spent endpoint on every request, paying
its latency and its error every time.

This module supplies both:

* :func:`classify_exhaustion` decides whether a failure means "come back later"
  (HTTP 429, quota/rate-limit/billing messages) rather than "this is broken".
* :class:`ExhaustionTracker` remembers it for a cooldown, so routing can skip
  that provider and fail over immediately — and comes back on its own once the
  cooldown passes, without a permanent blacklist.

Cooldown duration is bounded and configurable. Nothing here blocks a request:
an exhausted provider is a routing hint, not a lock.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from forge.models.errors import ProviderExhaustedError

#: Default cooldown when the endpoint states no `Retry-After`.
DEFAULT_COOLDOWN_SECONDS = 60.0
#: A stated `Retry-After` is honoured but never allowed to exceed this.
MAX_COOLDOWN_SECONDS = 900.0

_QUOTA_PATTERNS = (
    re.compile(r"insufficient[_\s-]?quota", re.I),
    re.compile(r"quota[_\s-]?(?:exceeded|exhausted|spent|reached)", re.I),
    re.compile(r"exceeded your current quota", re.I),
    re.compile(r"out of credits|no credits|credit balance|billing", re.I),
    re.compile(r"rate[_\s-]?limit", re.I),
    re.compile(r"too many requests", re.I),
    re.compile(r"resource[_\s-]?exhausted", re.I),
    re.compile(r"capacity|overloaded", re.I),
)

_STATUS_QUOTA = frozenset({402, 429, 503})


@dataclass(frozen=True)
class QuotaSignal:
    """The verdict on one failure."""

    exhausted: bool
    retry_after: float | None = None
    reason: str = ""
    status: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "exhausted": self.exhausted,
            "retry_after": self.retry_after,
            "reason": self.reason,
            "status": self.status,
        }


def safe_attribute(error: BaseException, name: str) -> Any:
    """``getattr`` that cannot itself blow up while classifying a failure.

    A Python 3.8 ``urllib.error.HTTPError`` inherits an attribute delegate from
    the stdlib file wrapper whose ``__getattr__`` raises ``KeyError('file')``
    for anything it does not have — so a plain ``getattr(exc, "retry_after",
    None)`` raised instead of returning the default, and a *real* HTTP 429 from
    a provider could not be classified at all on that interpreter. Reading an
    optional attribute off somebody else's exception must never be the thing
    that fails, so the lookup is guarded here and reused by every caller that
    inspects a provider exception.
    """
    try:
        return getattr(error, name)
    except (AttributeError, KeyError, IndexError, TypeError):
        return None


def _retry_after_from(exc: BaseException) -> float | None:
    """Read a stated retry delay off an exception, when the provider kept it."""
    for attribute in ("retry_after", "retry_after_seconds"):
        value = safe_attribute(exc, attribute)
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)
    headers = safe_attribute(exc, "headers")
    if headers is not None:
        try:
            raw = headers.get("Retry-After") or headers.get("retry-after")
        except Exception:                                     # noqa: BLE001
            raw = None
        if raw:
            try:
                return max(0.0, float(str(raw).strip()))
            except ValueError:
                return None
    return None


def classify_exhaustion(error: BaseException | str) -> QuotaSignal:
    """Decide whether ``error`` means "this provider is spent, try another".

    Recognises the typed :class:`ProviderExhaustedError`, quota/rate-limit HTTP
    statuses, and the wording the common providers use. Everything else is a
    real failure and is *not* classified as exhaustion — a broken endpoint must
    not be mistaken for a full one.
    """
    if isinstance(error, ProviderExhaustedError):
        return QuotaSignal(
            True,
            retry_after=error.retry_after,
            reason=str(error) or "provider reported exhaustion",
            status=error.status,
        )

    message = error if isinstance(error, str) else str(error)
    status = safe_attribute(error, "code")
    if not isinstance(status, int):
        status = safe_attribute(error, "status")
    status = status if isinstance(status, int) else None

    matched = ""
    for pattern in _QUOTA_PATTERNS:
        if pattern.search(message):
            matched = pattern.pattern
            break

    if status in _STATUS_QUOTA and (matched or status in (402, 429)):
        return QuotaSignal(True, retry_after=_retry_after_from(error)
                           if isinstance(error, BaseException) else None,
                           reason=message.strip()[:300] or f"HTTP {status}",
                           status=status)
    if matched and not isinstance(error, str):
        return QuotaSignal(True, retry_after=_retry_after_from(error),
                           reason=message.strip()[:300], status=status)
    if matched and isinstance(error, str):
        return QuotaSignal(True, reason=message.strip()[:300], status=status)
    return QuotaSignal(False, reason=message.strip()[:300], status=status)


@dataclass
class _Cooldown:
    until: float
    reason: str
    recorded_at: float


@dataclass
class ExhaustionTracker:
    """Remembers which providers are spent, until their cooldown passes."""

    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    max_cooldown_seconds: float = MAX_COOLDOWN_SECONDS
    #: Injectable clock, so cooldown expiry is testable without sleeping.
    clock: Any = time.monotonic
    _entries: dict[tuple[str, str], _Cooldown] = field(default_factory=dict)
    #: Set by the owner (the fabric) to record telemetry events.
    on_exhausted: Any = None

    def _key(self, provider: str, model: str = "") -> tuple[str, str]:
        return (str(provider), str(model))

    def cooldown_for(self, signal: QuotaSignal) -> float:
        stated = signal.retry_after
        if stated is None or stated <= 0:
            return max(0.0, float(self.cooldown_seconds))
        return min(float(stated), float(self.max_cooldown_seconds))

    def record(self, provider: str, model: str, signal: QuotaSignal,
               *, cooldown: float | None = None) -> float:
        """Mark ``provider``/``model`` exhausted; returns the cooldown applied."""
        if not signal.exhausted:
            return 0.0
        seconds = self.cooldown_for(signal) if cooldown is None else max(0.0, cooldown)
        now = self.clock()
        self._entries[self._key(provider, model)] = _Cooldown(
            until=now + seconds, reason=signal.reason, recorded_at=now)
        if callable(self.on_exhausted):
            self.on_exhausted(provider, model, signal, seconds)
        return seconds

    def clear(self, provider: str, model: str = "") -> None:
        self._entries.pop(self._key(provider, model), None)

    def is_exhausted(self, provider: str, model: str = "") -> bool:
        entry = self._entries.get(self._key(provider, model))
        if entry is None:
            return False
        if self.clock() >= entry.until:
            # Cooldown passed: the provider gets another chance by itself.
            self._entries.pop(self._key(provider, model), None)
            return False
        return True

    def remaining(self, provider: str, model: str = "") -> float:
        entry = self._entries.get(self._key(provider, model))
        if entry is None:
            return 0.0
        return max(0.0, entry.until - self.clock())

    def prune(self) -> None:
        now = self.clock()
        for key in [key for key, entry in self._entries.items() if now >= entry.until]:
            self._entries.pop(key, None)

    def snapshot(self) -> dict[str, Any]:
        """Current state, for readiness output and the API surface."""
        self.prune()
        exhausted = [
            {
                "provider": provider,
                "model": model,
                "seconds_remaining": round(self.remaining(provider, model), 1),
                "reason": entry.reason,
            }
            for (provider, model), entry in sorted(self._entries.items())
        ]
        return {
            "exhausted_count": len(exhausted),
            "exhausted": exhausted,
            "default_cooldown_seconds": self.cooldown_seconds,
            "max_cooldown_seconds": self.max_cooldown_seconds,
        }
