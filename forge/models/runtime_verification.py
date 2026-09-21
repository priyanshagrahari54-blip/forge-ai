"""Runtime verification for exact Model Fabric model/provider identities.

Discovery and configuration never imply liveness. This module provides a
small provider-agnostic verification contract that adapters can implement.
A successful *inference* probe activates the exact registry entry; failures
produce an explicit health state without inventing a usable model.

Evidence vocabulary (weakest to strongest):

* ``discovered`` — the provider *lists* the model id. Existence evidence only:
  it never activates a model and never revokes an earlier inference verdict.
* ``not_found``/``unhealthy`` — the provider does not list the model, or an
  inference attempt failed. Conclusive negative evidence.
* ``healthy``/``verified``/``live`` — a real inference returned output for the
  exact model id. The only evidence that makes a model routable.
* inconclusive — the probe could not be performed (no list endpoint, transport
  failure). Absence of evidence: the model is left exactly as it was.
"""
from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from time import monotonic
from typing import Any, Callable

from forge.models.health import HealthStatus
from forge.models.registry import Model, ModelRegistry

#: Probe statuses that carry an inference-grade verdict.
VERIFIED_STATUSES = frozenset({"healthy", "verified", "live"})
#: Probe status used by discovery-only checks (``list_models`` contains the id).
DISCOVERED_STATUS = "discovered"
#: Probe status used when discovery conclusively shows the id is not served.
NOT_FOUND_STATUS = "not_found"

_PROBE_PROMPT = "Reply with exactly OK. This is a Forge runtime verification probe."
_PROBE_TASK = "Forge runtime verification."
#: Verification only needs proof that the model answers; keep the spend tiny.
PROBE_MAX_OUTPUT_TOKENS = 8


@dataclass(frozen=True)
class RuntimeProbeResult:
    """Evidence produced by an adapter-supplied runtime probe.

    ``conclusive`` separates "the probe ran and observed the model" from
    "the probe could not be performed at all". An inconclusive probe is
    *absence of evidence*: it must never be recorded as a health verdict or
    used to take a working model out of routing. Adapters that genuinely
    cannot enumerate models (custom HTTP providers, scripted providers,
    gateways without a list endpoint) therefore stay honest instead of
    being reported as unavailable.
    """

    model_id: str
    ok: bool
    latency_ms: float = 0.0
    status: str = ""
    reason: str = ""
    capabilities: tuple[str, ...] = ()
    context_window: int | None = None
    max_output_tokens: int | None = None
    conclusive: bool = True

    @property
    def verified(self) -> bool:
        """True only for an inference-grade success."""
        return bool(self.ok) and str(self.status or "").lower() in VERIFIED_STATUSES

    @property
    def discovered(self) -> bool:
        """True when the probe only observed the model id in an inventory."""
        return bool(self.ok) and str(self.status or "").lower() == DISCOVERED_STATUS

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
            "conclusive": self.conclusive,
        }


def _set_if_settable(instance: Any, attribute: str, value: Any,
                     *, required: bool = False) -> bool:
    """Write ``attribute`` when the target actually carries it.

    Real :class:`~forge.models.registry.Model` objects declare every field
    this module writes. Duck-typed registries (test doubles, third-party
    adapters) may expose only a subset; missing optional fields are skipped
    instead of raising, while ``required`` attributes are still attempted so
    a failure surfaces where it matters.
    """
    if not required and not hasattr(instance, attribute):
        return False
    try:
        setattr(instance, attribute, value)
    except Exception:
        return False
    return True


def _metadata(model: Any) -> dict[str, Any] | None:
    metadata = getattr(model, "metadata", None)
    return metadata if isinstance(metadata, dict) else None


def is_inference_verified(model: Any) -> bool:
    """True when the registry entry carries an inference-grade verdict."""
    metadata = _metadata(model)
    return bool(metadata and metadata.get("runtime_verified") is True)


def _apply_verified(model: Any, result: RuntimeProbeResult, *,
                    count_success: bool = True) -> None:
    _set_if_settable(model, "available", True, required=True)
    _set_if_settable(model, "latency_ms", max(0.0, result.latency_ms))
    health = getattr(model, "health", None)
    if health is not None:
        record = getattr(health, "record_success", None)
        #: A probe is a success nothing else records; production traffic is
        #: already counted by the fabric's own feedback, so it is not counted
        #: twice here.
        if count_success and callable(record):
            record()
        #: ``ModelHealth.status`` is the closed ``HealthStatus`` vocabulary
        #: (``provider_health`` aggregates on it); the probe's own status
        #: lives in ``metadata``.
        health.status = HealthStatus.HEALTHY.value
        health.last_error = ""
    metadata = _metadata(model)
    if metadata is not None:
        metadata["runtime_verified"] = True
        metadata["verification_kind"] = "inference"
        metadata["last_verified"] = time.time()
        metadata["last_probe"] = result.to_dict()
    if result.capabilities:
        _set_if_settable(model, "capabilities", tuple(dict.fromkeys(result.capabilities)))
        _set_if_settable(model, "capability_status", {
            capability: "verified"
            for capability in getattr(model, "capabilities", ()) or ()
        })
    if result.context_window is not None:
        _set_if_settable(model, "context_window", max(1, result.context_window))
    if result.max_output_tokens is not None:
        _set_if_settable(model, "max_output_tokens", max(1, result.max_output_tokens))


def _apply_discovered(model: Any, result: RuntimeProbeResult) -> None:
    """Discovery only proves the id exists: it never activates or revokes.

    ``available`` is left untouched on purpose. Routability changes only on
    conclusive verdicts (an inference success or failure, or the id vanishing
    from the inventory); the monitor follows a discovery hit with a bounded
    inference probe that supplies that verdict.
    """
    metadata = _metadata(model)
    if metadata is not None:
        metadata["discovered"] = True
        metadata["last_discovered"] = time.time()
        metadata["last_probe"] = result.to_dict()
        if not is_inference_verified(model):
            metadata["runtime_verified"] = False
            metadata["verification_kind"] = DISCOVERED_STATUS


def _apply_failure(model: Any, result: RuntimeProbeResult) -> None:
    _set_if_settable(model, "available", False, required=True)
    _set_if_settable(model, "latency_ms", max(0.0, result.latency_ms))
    health = getattr(model, "health", None)
    if health is not None:
        if result.ok is False:
            fail = getattr(health, "record_failure", None)
            if callable(fail):
                fail(result.reason)
            health.last_error = result.reason
        health.status = HealthStatus.UNHEALTHY.value
    metadata = _metadata(model)
    if metadata is not None:
        metadata["runtime_verified"] = False
        metadata["verification_kind"] = str(result.status or "unknown").lower()
        metadata["last_probe"] = result.to_dict()
    reliability = getattr(model, "reliability", None)
    if isinstance(reliability, (int, float)) and result.ok is False:
        _set_if_settable(model, "reliability", max(0.0, reliability * 0.8))


def apply_probe_result(registry: ModelRegistry,
                       result: RuntimeProbeResult) -> "Model | None":
    """Apply a probe to an existing model; never creates unknown identities.

    Returns the updated model, or ``None`` when the registry cannot resolve
    the probed identity (a duck-typed registry used by an adapter). A
    registry that cannot resolve the model is not evidence about the model,
    so callers keep the probe's runtime verdict.
    """
    try:
        model = registry.get(result.model_id)
    except Exception:
        return None
    if not result.conclusive:
        # Inconclusive evidence is not a health verdict: leave the model
        # exactly as it was rather than inventing availability *or* failure.
        return model
    if result.verified:
        _apply_verified(model, result)
    elif result.discovered:
        _apply_discovered(model, result)
    else:
        _apply_failure(model, result)
    return model


def record_inference_success(model: Any, *, latency_ms: float = 0.0,
                             source: str = "generation") -> None:
    """Record that a real generation succeeded for ``model``.

    Production traffic is the strongest verification evidence there is: the
    exact model answered a real request. The registry entry is activated and
    stamped so the runtime monitor can promote it without a separate probe.
    """
    _apply_verified(model, RuntimeProbeResult(
        model_id=str(getattr(model, "name", "")),
        ok=True,
        latency_ms=max(0.0, float(latency_ms or 0.0)),
        status=HealthStatus.HEALTHY.value,
        reason=f"real inference succeeded ({source})",
    ), count_success=False)
    metadata = _metadata(model)
    if metadata is not None:
        metadata["verification_source"] = source


def _accepted_parameters(callable_obj: Any) -> set[str]:
    try:
        parameters = inspect.signature(callable_obj).parameters.values()
    except (TypeError, ValueError):
        return set()
    return {
        parameter.name for parameter in parameters
        if parameter.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                              inspect.Parameter.KEYWORD_ONLY)
    }


def probe_provider_inference(provider: Any, model_id: str, *,
                             provider_model_id: str | None = None,
                             max_output_tokens: int | None = PROBE_MAX_OUTPUT_TOKENS) -> RuntimeProbeResult:
    """Perform one bounded real inference probe against an exact provider/model.

    The probe is deliberately tiny (a few output tokens) and never part of a
    hot loop: the monitor runs it once per configured runtime, then backs off
    on failure, so a paid provider is not charged on every tick. It requires a
    non-empty response and, when the adapter reports which model answered,
    refuses a response from a different model.

    ``provider_model_id`` is the id the provider itself knows the model by
    (``Model.provider_model_id``); without it the registry namespace prefix is
    stripped heuristically.
    """
    started = monotonic()
    try:
        configured = str(getattr(provider, "model", "") or "")
        expected = provider_model_id or (
            model_id.split("/", 1)[1] if "/" in model_id else model_id)
        accepted = _accepted_parameters(getattr(provider, "generate", None))
        kwargs: dict[str, Any] = {"task": _PROBE_TASK}
        if "model" in accepted:
            kwargs["model"] = expected
        elif configured and configured != expected and configured != model_id:
            return RuntimeProbeResult(
                model_id=model_id,
                ok=False,
                latency_ms=(monotonic() - started) * 1000.0,
                status=HealthStatus.UNHEALTHY.value,
                reason="provider is configured for a different model",
            )
        if max_output_tokens is not None and "max_output_tokens" in accepted:
            kwargs["max_output_tokens"] = int(max_output_tokens)
        result = provider.generate(_PROBE_PROMPT, **kwargs)
        returned = str(getattr(result, "model", "") or "")
        #: Adapters that do not know model identities (deterministic and
        #: scripted providers) echo their *provider* name; that is absence of
        #: an identity claim, not a claim of a different model.
        no_identity = {"", str(getattr(provider, "name", "") or "")}
        if returned not in no_identity and returned not in {model_id, expected, configured}:
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
