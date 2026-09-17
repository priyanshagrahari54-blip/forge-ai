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


def test_verified_runtime_is_the_only_non_fallback_routable_model(tmp_path):
    registry = ModelRegistry()
    fabric = ModelFabric(registry=registry)
    provider = FakeProvider(["model-live"])
    fabric.register_provider("fake", provider)
    fabric.register_model(Model(name="model-live", provider="fake", capabilities=("coding",)))
    service = RuntimeMonitorService(fabric, state_path=tmp_path / "runtime.json")

    service.tick(force=True, now=100.0)
    assert service.registry.get("fake:model-live").state == RuntimeState.LIVE.value
    assert registry.get("model-live").available is True

    provider.models = []
    service.tick(force=True, now=401.0)
    assert service.registry.get("fake:model-live").state == RuntimeState.UNAVAILABLE.value
    assert registry.get("model-live").available is False
