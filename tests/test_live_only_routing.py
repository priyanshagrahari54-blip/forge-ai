from forge.models.registry import Model, ModelRegistry
from forge.models.request import ModelRequest
from forge.models.router import FabricRouter


def test_router_never_selects_unavailable_runtime():
    registry = ModelRegistry()
    registry.register(Model(
        name="verified-model",
        provider="provider-a",
        capabilities=("coding",),
        available=True,
        local=False,
        free=False,
    ))
    registry.register(Model(
        name="offline-model",
        provider="provider-b",
        capabilities=("coding",),
        available=False,
        local=True,
        free=True,
    ))

    decision = FabricRouter(registry).route(ModelRequest(capability="coding"))

    assert decision.model is not None
    assert decision.model.name == "verified-model"
    assert "offline-model" not in decision.candidates


def test_router_reports_no_model_when_only_runtime_is_unavailable():
    registry = ModelRegistry()
    registry.register(Model(
        name="offline-model",
        provider="provider-a",
        capabilities=("coding",),
        available=False,
    ))

    decision = FabricRouter(registry).route(ModelRequest(capability="coding"))

    assert decision.model is None
    assert decision.error
