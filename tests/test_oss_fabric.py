from forge.models.oss_fabric import (
    activate_oss_model,
    capabilities_for_oss_model,
    model_from_oss,
    register_oss_catalog,
)
from forge.models.oss_registry import OSSModel
from forge.models.registry import ModelRegistry
from forge.models.router import FabricRouter
from forge.models.request import ModelRequest


def test_oss_tags_map_only_to_canonical_capabilities():
    model = OSSModel(
        "org/code-vision",
        "org",
        "huggingface",
        tags=("coding", "vision", "embeddings", "unknown-tag"),
    )
    assert capabilities_for_oss_model(model) == ("coding", "vision")


def test_catalogued_model_is_not_live_by_default():
    model = model_from_oss(
        OSSModel("org/real-model", "org", "huggingface", tags=("coding",))
    )
    assert model.name == "org/real-model"
    assert model.provider == "huggingface"
    assert model.capabilities == ("coding",)
    assert model.available is False
    assert model.metadata["runtime_verified"] is False


def test_register_and_activate_exact_model():
    registry = ModelRegistry()
    source = OSSModel("org/real-model", "org", "huggingface", tags=("coding",))
    assert register_oss_catalog(registry, [source]) == 1
    assert register_oss_catalog(registry, [source]) == 0
    assert registry.get("org/real-model").available is False

    activated = activate_oss_model(
        registry,
        "org/real-model",
        context_window=32768,
        capabilities=("coding", "reasoning"),
        local=False,
    )
    assert activated.available is True
    assert activated.context_window == 32768
    assert activated.capabilities == ("coding", "reasoning")
    assert activated.capability_status == {"coding": "verified", "reasoning": "verified"}
    assert activated.metadata["runtime_verified"] is True


def test_fabric_routes_to_activated_real_model_only():
    registry = ModelRegistry()
    register_oss_catalog(
        registry,
        [OSSModel("org/code-model", "org", "huggingface", tags=("coding",))],
    )
    router = FabricRouter(registry=registry)
    request = ModelRequest(capability="coding", required_capabilities=("coding",))
    assert router.route(request).model is None

    activate_oss_model(registry, "org/code-model", capabilities=("coding",))
    decision = router.route(request)
    assert decision.model is not None
    assert decision.model.name == "org/code-model"


def test_activation_never_creates_unknown_model():
    registry = ModelRegistry()
    try:
        activate_oss_model(registry, "missing/model")
    except KeyError:
        pass
    else:
        raise AssertionError("activation must not create an unknown model")
