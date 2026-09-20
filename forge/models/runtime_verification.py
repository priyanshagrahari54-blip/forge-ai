"""Runtime verification for exact Model Fabric model/provider identities.

Discovery and configuration never imply liveness. This module provides a
small provider-agnostic verification contract that adapters can implement.
A successful probe activates the exact registry entry; failures produce an
explicit health state without inventing a usable model.
"""
from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Any, Callable

from forge.models.health import HealthStatus
from forge.models.registry import Model, ModelRegistry


@dataclass(frozen=True)
class RuntimeProbeResult:
    model_id: str
    ok: bool
    latency_ms: float
    status: str
    reason: str = ""
    capabilities: tuple[str, ...] = ()
    context_window: int | None = None
    max_output_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "ok": self.ok,
            "latency_ms": round(self.latency_ms, 2),
            "status": self.status,
            "reason": self.reason,
            "capabilities": list(self.capabilities),
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
        }


def apply_probe_result(registry: ModelRegistry, result: RuntimeProbeResult) -> Model:
    """Apply a probe to an existing model; never creates unknown identities."""
    model = registry.get(result.model_id)
    verified = bool(result.ok) and str(result.status or "").lower() in {"healthy", "verified", "live"}
    model.available = verified
    model.latency_ms = max(0.0, result.latency_ms)
    model.health.status = result.status
    model.health.last_error = "" if result.ok else result.reason
    model.metadata["runtime_verified"] = verified
    model.metadata["verification_kind"] = "inference" if verified else str(result.status or "unknown").lower()
    model.metadata["last_probe"] = result.to_dict()
    if verified:
        model.health.record_success()
        if result.capabilities:
            model.capabilities = tuple(dict.fromkeys(result.capabilities))
            model.capability_status = {capability: "verified" for capability in model.capabilities}
        if result.context_window is not None:
            model.context_window = max(1, result.context_window)
        if result.max_output_tokens is not None:
            model.max_output_tokens = max(1, result.max_output_tokens)
    else:
        model.health.record_failure(result.reason)
        model.reliability = max(0.0, model.reliability * 0.8)
    return model


def probe_provider_inference(provider: Any, model_id: str) -> RuntimeProbeResult:
    """Perform one bounded real inference probe against an exact provider/model.

    This is intentionally explicit rather than part of the periodic health loop:
    calling a paid provider every monitor tick would create hidden usage/cost.
    The probe requires a non-empty model response and, when the adapter exposes
    its model identity, refuses a response from a different model.
    """
    started = monotonic()
    try:
        configured = str(getattr(provider, "model", "") or "")
        expected = model_id.split("/", 1)[1] if "/" in model_id else model_id
        if configured and configured != expected and configured != model_id:
            return RuntimeProbeResult(
                model_id=model_id,
                ok=False,
                latency_ms=(monotonic() - started) * 1000.0,
                status=HealthStatus.UNHEALTHY.value,
                reason="provider is configured for a different model",
            )
        result = provider.generate(
            "Reply with exactly OK. This is a Forge runtime verification probe.",
            task="Forge runtime verification.",
        )
        returned = str(getattr(result, "model", "") or "")
        if returned and returned not in {model_id, expected, configured}:
            return RuntimeProbeResult(
                model_id=model_id,
                ok=False,
                latency_ms=(monotonic() - started) * 1000.0,
                status=HealthStatus.UNHEALTHY.value,
                reason="provider returned a different model identity",
            )
        response_text = str(getattr(result, "text", "") or "").strip()
        if not response_text:
            return RuntimeProbeResult(
                model_id=model_id,
                ok=False,
                latency_ms=(monotonic() - started) * 1000.0,
                status=HealthStatus.UNHEALTHY.value,
                reason="provider returned an empty inference response",
            )
        return RuntimeProbeResult(
            model_id=model_id,
            ok=True,
            latency_ms=(monotonic() - started) * 1000.0,
            status=HealthStatus.HEALTHY.value,
            reason="real inference probe succeeded",
        )
    except Exception as exc:
        return RuntimeProbeResult(
            model_id=model_id,
            ok=False,
            latency_ms=(monotonic() - started) * 1000.0,
            status=HealthStatus.UNHEALTHY.value,
            reason=f"probe-error:{type(exc).__name__}:{exc}",
        )

def verify_model(
    registry: ModelRegistry,
    model_id: str,
    probe: Callable[[Model], RuntimeProbeResult],
) -> RuntimeProbeResult:
    """Run a real adapter-supplied probe and feed its evidence into the registry."""
    model = registry.get(model_id)
    started = monotonic()
    try:
        result = probe(model)
    except Exception as exc:
        result = RuntimeProbeResult(
            model_id=model_id,
            ok=False,
            latency_ms=(monotonic() - started) * 1000.0,
            status=HealthStatus.UNHEALTHY.value,
            reason=f"probe-error:{type(exc).__name__}:{exc}",
        )
    if result.model_id != model_id:
        raise ValueError("probe returned a different model identity")
    apply_probe_result(registry, result)
    return result
