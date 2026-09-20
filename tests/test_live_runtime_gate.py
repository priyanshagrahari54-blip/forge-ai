from forge.models.configured_runtime import RuntimeState
from forge.models.fabric import ModelFabric
from forge.models.provider import MockProvider
from forge.models.registry import Model, ModelRegistry
from forge.models.runtime_monitor_service import RuntimeMonitorService


class FakeProvider(MockProvider):
    def __init__(self, models):
        super().__init__("ok")
        self.models = models

    def list_models(self):
        return list(self.models)


def test_discovered_runtime_is_not_routable_until_inference_is_verified(tmp_path):
    registry = ModelRegistry()
    fabric = ModelFabric(registry=registry)
    provider = FakeProvider(["model-live"])
    fabric.register_provider("fake", provider)
    fabric.register_model(Model(name="model-live", provider="fake", capabilities=("coding",)))
    service = RuntimeMonitorService(fabric, state_path=tmp_path / "runtime.json")

    service.tick(force=True, now=100.0)
    assert service.registry.get("fake:model-live").state == RuntimeState.CONFIGURED.value
    assert registry.get("model-live").available is False

    provider.models = []
    service.tick(force=True, now=401.0)
    assert service.registry.get("fake:model-live").state == RuntimeState.UNAVAILABLE.value
    assert registry.get("model-live").available is False
