from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Callable, Iterable


def _attribute(error: BaseException, name: str):
    """Optional attribute of somebody else's exception, or ``None``."""
    try:
        return getattr(error, name)
    except (AttributeError, KeyError, IndexError, TypeError):
        return None


@dataclass
class ProviderRuntime:
    """Runtime state used for honest provider/model failover.

    A provider is never treated as available merely because it is registered.
    Cooldowns are local Forge state and do not bypass provider quotas.
    """

    name: str
    failures: int = 0
    successes: int = 0
    cooldown_until: float = 0.0
    last_error: str = ""
    last_reason: str = ""

    @property
    def cooling_down(self) -> bool:
        return monotonic() < self.cooldown_until

    def mark_success(self) -> None:
        self.successes += 1
        self.failures = max(0, self.failures - 1)
        self.cooldown_until = 0.0
        self.last_error = ""
        self.last_reason = ""

    def mark_failure(self, reason: str, *, retry_after: float | None = None) -> None:
        self.failures += 1
        self.last_error = reason
        self.last_reason = reason
        if retry_after is not None:
            self.cooldown_until = max(self.cooldown_until, monotonic() + max(0.0, retry_after))


@dataclass(frozen=True)
class FailoverAttempt:
    candidate: str
    provider: str
    success: bool
    reason: str = ""


@dataclass
class FailoverResult:
    value: Any = None
    attempts: list[FailoverAttempt] = field(default_factory=list)
    exhausted: bool = False


class FailoverPool:
    """Run an operation across an ordered pool without silently losing state.

    The operation receives one candidate at a time. If a transient, quota, or
    provider-unavailable error is reported, the next eligible candidate is tried.
    Permanent policy/authentication errors can stop the chain immediately.
    """

    RETRYABLE = frozenset({"rate_limited", "quota_exhausted", "unavailable", "timeout", "transient"})

    def __init__(self) -> None:
        self.providers: dict[str, ProviderRuntime] = {}

    def state(self, provider: str) -> ProviderRuntime:
        return self.providers.setdefault(provider, ProviderRuntime(provider))

    def run(
        self,
        candidates: Iterable[tuple[str, str]],
        operation: Callable[[str, str], Any],
    ) -> FailoverResult:
        result = FailoverResult()
        for candidate, provider in candidates:
            runtime = self.state(provider)
            if runtime.cooling_down:
                result.attempts.append(FailoverAttempt(candidate, provider, False, "cooldown"))
                continue
            try:
                value = operation(candidate, provider)
            except Exception as exc:  # provider adapters normalize errors upstream
                #: Attribute reads are guarded: a Python 3.8 urllib HTTPError
                #: raises KeyError (not AttributeError) for attributes it does
                #: not have, and a failover decision must survive that.
                reason = _attribute(exc, "reason") or "transient"
                retry_after = _attribute(exc, "retry_after")
                runtime.mark_failure(str(exc), retry_after=retry_after)
                result.attempts.append(FailoverAttempt(candidate, provider, False, reason))
                if reason not in self.RETRYABLE:
                    result.exhausted = True
                    return result
                continue
            runtime.mark_success()
            result.value = value
            result.attempts.append(FailoverAttempt(candidate, provider, True))
            return result
        result.exhausted = True
        return result

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "provider": state.name,
                "status": "cooldown" if state.cooling_down else "available",
                "failures": state.failures,
                "successes": state.successes,
                "last_error": state.last_error,
                "last_reason": state.last_reason,
            }
            for state in sorted(self.providers.values(), key=lambda item: item.name)
        ]
