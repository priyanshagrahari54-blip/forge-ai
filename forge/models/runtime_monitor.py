"""Durable-friendly runtime health monitoring and promotion coordinator."""
from __future__ import annotations

from dataclasses import dataclass
from time import time
from typing import Callable, Iterable, Optional
from uuid import uuid4

from forge.models.configured_runtime import ConfiguredRuntime, RuntimeState
from forge.models.health import HealthStatus
from forge.models.runtime_verification import RuntimeProbeResult


def _bounded_reason(reason: str, limit: int = 500) -> str:
    """Normalize a bounded, secret-free reason string."""
    text = " ".join(str(reason or "").split())
    return text[:limit]


@dataclass(frozen=True)
class RuntimeMonitorResult:
    provider: str
    model_id: str
    state: str
    probe: RuntimeProbeResult
    verification_id: str = ""
    changed: bool = False

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "state": self.state,
            "probe": self.probe.to_dict(),
            "verification_id": self.verification_id,
            "changed": self.changed,
        }


class RuntimeMonitor:
    """Apply fresh exact-model probe evidence to configured runtimes."""

    def __init__(self, *, verification_ttl_seconds: float = 300.0) -> None:
        self.verification_ttl_seconds = max(1.0, float(verification_ttl_seconds))

    def check(
        self,
        runtime: ConfiguredRuntime,
        probe: Callable[[str, str], RuntimeProbeResult],
        *,
        now: Optional[float] = None,
    ) -> RuntimeMonitorResult:
        checked = float(time() if now is None else now)
        result = probe(runtime.provider, runtime.model_id)
        if result.model_id != runtime.model_id:
            raise ValueError("probe returned a different model identity")

        if not result.conclusive:
            # The probe could not be performed. That is absence of evidence,
            # not evidence of failure: keep the runtime's last known state so
            # a working provider without a model-list endpoint stays routable,
            # and record honestly that verification was inconclusive. The
            # timestamp is intentionally *not* refreshed, so no fresh
            # verification claim is minted from a probe that never ran.
            runtime.last_reason = _bounded_reason(
                "runtime verification inconclusive: %s" % (result.reason or result.status))
            return RuntimeMonitorResult(
                runtime.provider, runtime.model_id, runtime.state, result,
                runtime.verification_id, False,
            )

        if not result.ok:
            runtime.mark_unavailable(result.reason)
            runtime.last_checked = checked
            return RuntimeMonitorResult(
                runtime.provider, runtime.model_id, runtime.state, result,
                runtime.verification_id, True,
            )

        verification_id = str(uuid4())
        if runtime.state == RuntimeState.UNAVAILABLE.value:
            # Recovery is explicit: a fresh successful probe is allowed to
            # re-enter the normal CONFIGURED -> VERIFIED -> LIVE lifecycle.
            runtime.set_configured(valid=True)
        if runtime.state == RuntimeState.CONFIGURED.value:
            runtime.mark_verified(
                verification_id=verification_id,
                capabilities=result.capabilities,
                checked_at=checked,
            )
            runtime.activate()
        elif runtime.state == RuntimeState.VERIFIED.value:
            runtime.verification_id = verification_id
            runtime.capabilities = tuple(dict.fromkeys(result.capabilities))
            runtime.last_checked = checked
            runtime.activate()
        elif runtime.state == RuntimeState.LIVE.value:
            runtime.verification_id = verification_id
            runtime.capabilities = tuple(dict.fromkeys(result.capabilities))
            runtime.last_checked = checked
        else:
            raise RuntimeError(
                "runtime in %s cannot be promoted by a probe" % runtime.state)
        runtime.last_reason = ""
        return RuntimeMonitorResult(
            runtime.provider, runtime.model_id, runtime.state, result,
            runtime.verification_id, True,
        )

    def stale(self, runtime: ConfiguredRuntime, *, now: Optional[float] = None) -> bool:
        if runtime.state != RuntimeState.LIVE.value:
            return True
        checked = float(time() if now is None else now)
        return (runtime.last_checked <= 0 or
                checked - runtime.last_checked > self.verification_ttl_seconds)

    @staticmethod
    def routable(runtimes: Iterable[ConfiguredRuntime]) -> list:
        """Return only explicitly LIVE runtimes for routing consumers."""
        return [runtime for runtime in runtimes if runtime.routable]

    @staticmethod
    def health_status(result: RuntimeProbeResult) -> str:
        return (HealthStatus.HEALTHY.value if result.ok
                else HealthStatus.UNHEALTHY.value)
