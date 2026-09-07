import pytest

from forge.models.health import HealthStatus
from forge.models.registry import Model, ModelRegistry


def make_model(name, provider="p", capabilities=("coding",)):
    return Model(name=name, provider=provider, capabilities=capabilities)


def test_register_and_lookup():
    registry = ModelRegistry()
    registry.register(make_model("a"))
    assert registry.has("a")
    assert registry.get("a").name == "a"
    assert "a" in registry
    assert len(registry) == 1


def test_duplicate_registration_rejected():
    registry = ModelRegistry([make_model("a")])
    with pytest.raises(ValueError):
        registry.register(make_model("a"))


def test_invalid_model_rejected():
    registry = ModelRegistry()
    with pytest.raises(ValueError):
        registry.register(Model(name="", provider="p"))
    with pytest.raises(ValueError):
        registry.register(Model(name="x", provider=""))
    with pytest.raises(ValueError):
        registry.register(Model(name="x", provider="p", context_window=0))


def test_unknown_capability_rejected():
    with pytest.raises(ValueError):
        Model(name="x", provider="p", capabilities=("not-a-capability",))


def test_capability_lookup_and_availability():
    coding = make_model("coder", capabilities=("coding", "debugging"))
    vision = make_model("seer", capabilities=("vision",))
    offline = Model(name="off", provider="p", capabilities=("coding",), available=False)
    registry = ModelRegistry([coding, vision, offline])

    assert [m.name for m in registry.by_capability("coding")] == ["coder", "off"]
    assert [m.name for m in registry.available()] == ["coder", "seer"]
    assert [m.name for m in registry.models_for_capabilities(("coding", "debugging"))] == ["coder"]
    assert registry.models_for_capabilities(("coding", "vision")) == []
    assert registry.capabilities() == ["coding", "debugging", "vision"]


def test_remove_and_roundtrip():
    registry = ModelRegistry([make_model("a")])
    registry.remove("a")
    assert not registry.has("a")
    with pytest.raises(KeyError):
        registry.remove("a")


def test_model_dict_roundtrip_preserves_fallback_and_health():
    model = Model(
        name="local-fallback", provider="local",
        capabilities=("coding", "reasoning"), context_window=4096,
        free=True, local=True, fallback=True,
    )
    model.health.record_failure("boom")
    restored = Model.from_dict(model.to_dict())
    assert restored.name == model.name
    assert restored.fallback is True
    assert restored.health.status == HealthStatus.UNKNOWN.value  # 1 failure < degrade_after
    assert restored.health.total_failures == 1


def test_snapshot_is_deterministic():
    registry = ModelRegistry([make_model("b"), make_model("a")])
    names = [entry["name"] for entry in registry.snapshot()]
    assert names == ["a", "b"]
