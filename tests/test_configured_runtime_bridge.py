from forge.models.configured_runtime import RuntimeState
from forge.models.configured_runtime_bridge import sync_configured_runtimes
from forge.models.fabric import ModelFabric
from forge.models.provider import MockProvider, ProviderInfo
from forge.models.registry import Model, ModelRegistry


def test_sync_turns_registered_provider_model_into_configured_only():
    fabric = ModelFabric(registry=ModelRegistry())
    fabric.register_provider(
        "test-provider",
        MockProvider("ok"),
        ProviderInfo(name="test-provider", endpoint="https://example.invalid/v1"),
    )
    fabric.register_model(Model(
        name="test-provider/model-a",
        provider="test-provider",
        capabilities=("coding",),
    ))

    runtimes = sync_configured_runtimes(fabric)
    runtime = runtimes.get("test-provider:test-provider/model-a")

    assert runtime.state == RuntimeState.CONFIGURED.value
    assert not runtime.routable
    assert runtime.provider == "test-provider"
    assert runtime.model_id == "test-provider/model-a"


def test_sync_does_not_invent_models_or_promote_live():
    fabric = ModelFabric(registry=ModelRegistry())
    fabric.register_provider("provider", MockProvider("ok"))
    runtimes = sync_configured_runtimes(fabric)

    assert runtimes.snapshot() == []
    assert runtimes.live() == []


def test_sync_is_idempotent_and_preserves_verified_state():
    fabric = ModelFabric(registry=ModelRegistry())
    fabric.register_provider("provider", MockProvider("ok"))
    fabric.register_model(Model(name="model", provider="provider", capabilities=("coding",)))
    runtimes = sync_configured_runtimes(fabric)
    runtime = runtimes.get("provider:model")
    runtime.mark_verified(verification_id="probe-1")

    sync_configured_runtimes(fabric, runtimes)

    assert runtime.state == RuntimeState.VERIFIED.value
    assert runtime.verification_id == "probe-1"
    assert not runtime.routable
