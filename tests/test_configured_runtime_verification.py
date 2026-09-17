from forge.models.config import FabricConfig
from forge.models.configured_runtime import RuntimeState
from forge.models.configured_runtime_bridge import sync_configured_runtimes
from forge.models.configured_runtime_verification import verify_and_activate
from forge.models.fabric import ModelFabric
from forge.models.provider import MockProvider
from forge.models.registry import Model, ModelRegistry
from forge.models.runtime_verification import RuntimeProbeResult


def _fabric():
    fabric = ModelFabric(registry=ModelRegistry())
    fabric.register_provider("provider", MockProvider("ok"))
    fabric.register_model(Model(name="model", provider="provider", capabilities=("coding",)))
    return fabric


def test_successful_exact_probe_promotes_configured_to_live():
    fabric = _fabric()
    runtimes = sync_configured_runtimes(fabric)
    runtime = runtimes.get("provider:model")

    result = verify_and_activate(
        fabric,
        runtime,
        lambda model: RuntimeProbeResult(
            model_id=model.name,
            ok=True,
            latency_ms=4.0,
            status="HEALTHY",
            capabilities=("coding",),
        ),
        registry=runtimes,
        verification_id="probe-123",
    )

    assert result.ok
    assert runtime.state == RuntimeState.LIVE.value
    assert runtime.routable
    assert runtime.verification_id == "probe-123"


def test_failed_probe_never_activates_runtime():
    fabric = _fabric()
    runtimes = sync_configured_runtimes(fabric)
    runtime = runtimes.get("provider:model")

    result = verify_and_activate(
        fabric,
        runtime,
        lambda model: RuntimeProbeResult(
            model_id=model.name,
            ok=False,
            latency_ms=8.0,
            status="UNHEALTHY",
            reason="connection refused",
        ),
        registry=runtimes,
    )

    assert not result.ok
    assert runtime.state == RuntimeState.UNAVAILABLE.value
    assert not runtime.routable


def test_verification_rejects_mismatched_identity():
    fabric = _fabric()
    runtimes = sync_configured_runtimes(fabric)
    runtime = runtimes.get("provider:model")

    try:
        verify_and_activate(
            fabric,
            runtime,
            lambda model: RuntimeProbeResult(
                model_id="different-model",
                ok=True,
                latency_ms=1.0,
                status="HEALTHY",
            ),
            registry=runtimes,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("mismatched model identity must be rejected")

    assert runtime.state == RuntimeState.CONFIGURED.value
