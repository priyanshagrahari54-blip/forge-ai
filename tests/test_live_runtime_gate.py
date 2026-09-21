from forge.models.configured_runtime import RuntimeState
from forge.models.fabric import ModelFabric
from forge.models.provider import MockProvider, ModelResult
from forge.models.registry import Model, ModelRegistry
from forge.models.runtime_monitor_service import RuntimeMonitorService


class FakeProvider(MockProvider):
    """Lists a model, but answers from a different model until ``honest``."""

    def __init__(self, models):
        super().__init__("ok")
        self.models = models
        self.honest = False

    def list_models(self):
        return list(self.models)

    def generate(self, prompt, *, context="", task=""):
        if self.honest:
            return ModelResult("OK", "model-live")
        return ModelResult(self.response, "some-other-model")


def _fabric():
    registry = ModelRegistry()
    fabric = ModelFabric(registry=registry)
    provider = FakeProvider(["model-live"])
    fabric.register_provider("fake", provider)
    fabric.register_model(Model(name="model-live", provider="fake", capabilities=("coding",)))
    return fabric, registry, provider


def test_discovered_runtime_is_not_routable_until_inference_is_verified(tmp_path):
    fabric, registry, provider = _fabric()
    service = RuntimeMonitorService(fabric, state_path=tmp_path / "runtime.json",
                                    inference_retry_seconds=60)

    # Listed by the provider, but the bounded inference probe fails (the
    # answer comes from a different model identity): conclusive negative.
    service.tick(force=True, now=100.0)
    runtime = service.registry.get("fake:model-live")
    assert runtime.state == RuntimeState.UNAVAILABLE.value
    assert "different model identity" in runtime.last_reason
    assert registry.get("model-live").available is False
    assert registry.get("model-live").metadata["runtime_verified"] is False
    assert registry.get("model-live").metadata["discovered"] is True

    # Backoff: the failing runtime is not re-probed on the very next tick.
    provider.honest = True
    service.tick(force=True, now=130.0)
    assert service.registry.get("fake:model-live").state == RuntimeState.CONFIGURED.value
    assert registry.get("model-live").available is False

    # Once the backoff elapsed the real inference succeeds: routable.
    service.tick(force=True, now=200.0)
    assert service.registry.get("fake:model-live").state == RuntimeState.LIVE.value
    assert registry.get("model-live").available is True
    assert registry.get("model-live").metadata["runtime_verified"] is True
    assert registry.get("model-live").metadata["verification_kind"] == "inference"

    # The id vanishing from the inventory is conclusive negative evidence.
    provider.models = []
    service.tick(force=True, now=601.0)
    assert service.registry.get("fake:model-live").state == RuntimeState.UNAVAILABLE.value
    assert registry.get("model-live").available is False
    assert registry.get("model-live").metadata["runtime_verified"] is False


def test_discovery_alone_never_verifies_when_probes_are_disabled(tmp_path):
    fabric, registry, provider = _fabric()
    provider.honest = True
    service = RuntimeMonitorService(fabric, state_path=tmp_path / "runtime.json",
                                    inference_probes=False)

    service.tick(force=True, now=100.0)

    runtime = service.registry.get("fake:model-live")
    assert runtime.state == RuntimeState.CONFIGURED.value
    assert registry.get("model-live").metadata["runtime_verified"] is False
    assert registry.get("model-live").metadata["verification_kind"] == "discovered"
    assert service.snapshot()["providers"]["fake"]["discovered"] == 1
    assert service.snapshot()["providers"]["fake"]["live"] == 0
