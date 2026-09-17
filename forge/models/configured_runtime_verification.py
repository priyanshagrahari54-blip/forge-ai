"""Bridge configured runtimes into evidence-backed LIVE state.

This module is deliberately small: configuration discovers what Forge can
attempt, while a caller-supplied real probe supplies the evidence. A successful
probe for the exact provider/model binding promotes the configured runtime to
VERIFIED and then LIVE. Failures never activate a runtime.
"""
from __future__ import annotations

from time import time
from typing import Any, Callable

from forge.models.configured_runtime import ConfiguredRuntime, ConfiguredRuntimeRegistry, RuntimeState
from forge.models.runtime_verification import RuntimeProbeResult, verify_model


def verify_and_activate(
    fabric: Any,
    runtime: ConfiguredRuntime,
    probe: Callable[[Any], RuntimeProbeResult],
    *,
    registry: ConfiguredRuntimeRegistry | None = None,
    verification_id: str = "",
) -> RuntimeProbeResult:
    """Run an exact real probe and promote the matching configured runtime.

    The runtime must already be CONFIGURED. The model must already exist in
    Model Fabric's registry under the exact ``model_id``. A successful probe is
    the only path to LIVE; exceptions and failed probes leave the runtime
    unavailable and unroutable.
    """
    if runtime.state != RuntimeState.CONFIGURED.value:
        raise RuntimeError("runtime must be CONFIGURED before verification")
    model_registry = getattr(fabric, "registry", None)
    if model_registry is None:
        raise RuntimeError("fabric model registry is required")

    result = verify_model(model_registry, runtime.model_id, probe)
    runtime.last_checked = time()
    runtime.last_reason = result.reason
    if not result.ok:
        runtime.mark_unavailable(result.reason)
        return result

    runtime.mark_verified(
        verification_id=verification_id or "probe:%s:%d" % (runtime.model_id, int(time())),
        capabilities=result.capabilities,
        checked_at=time(),
    )
    runtime.activate()
    return result


def deactivate_on_failure(runtime: ConfiguredRuntime, result: RuntimeProbeResult) -> None:
    """Apply a failed probe to a previously live runtime without hiding why."""
    if result.ok:
        return
    runtime.mark_unavailable(result.reason)
