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


def test_capability_index_tracks_register_replace_remove():
    registry = ModelRegistry()
    registry.register(make_model("a", capabilities=("coding",)))
    registry.register(make_model("b", capabilities=("coding", "vision")))

    assert [m.name for m in registry.by_capability("coding")] == ["a", "b"]
    assert [m.name for m in registry.by_capability("vision")] == ["b"]
    assert [m.name for m in registry.models_for_capabilities(("coding", "vision"))] == ["b"]

    # Replacing with a different capability set re-indexes atomically.
    registry.replace(make_model("b", capabilities=("reasoning",)))
    assert [m.name for m in registry.by_capability("vision")] == []
    assert [m.name for m in registry.by_capability("reasoning")] == ["b"]
    assert [m.name for m in registry.by_capability("coding")] == ["a"]

    # Removing drops the index entries too.
    registry.remove("a")
    assert [m.name for m in registry.by_capability("coding")] == []
    assert registry.by_capability("reasoning")[0].name == "b"


def test_capability_index_self_heals_after_post_registration_mutation():
    """Verification/activation paths reassign ``model.capabilities`` in place.

    The index must never keep routing by the stale set: the next capability
    query reconciles before answering.
    """
    from forge.models.runtime_verification import RuntimeProbeResult, apply_probe_result

    registry = ModelRegistry([make_model("m", capabilities=("coding",))])
    assert [item.name for item in registry.by_capability("vision")] == []

    # Simulates a probe that upgrades the advertised capability set.
    apply_probe_result(registry, RuntimeProbeResult(
        "m", True, 1.0, "healthy", capabilities=("coding", "vision")))
    assert registry.get("m").capabilities == ("coding", "vision")
    assert [item.name for item in registry.by_capability("vision")] == ["m"]
    assert [item.name for item in registry.models_for_capabilities(("coding", "vision"))] == ["m"]

    # And a downgrade stops matching immediately as well.
    apply_probe_result(registry, RuntimeProbeResult(
        "m", True, 1.0, "healthy", capabilities=("coding",)))
    assert [item.name for item in registry.by_capability("vision")] == []


def test_indexed_queries_match_direct_scan():
    """The index must be observational-equivalent to the old full scan."""
    models = [
        make_model("m1", capabilities=("coding", "reasoning")),
        make_model("m2", capabilities=("coding",)),
        make_model("m3", capabilities=("vision", "audio")),
        make_model("m4", capabilities=("coding", "vision")),
    ]
    registry = ModelRegistry(models)
    for capability in ("coding", "reasoning", "vision", "audio", "security"):
        indexed = [m.name for m in registry.by_capability(capability)]
        scanned = sorted(m.name for m in models if capability in m.capabilities)
        assert indexed == scanned
    for required in (("coding",), ("coding", "vision"), ("vision", "audio"),
                     ("coding", "security"), ()):
        indexed = [m.name for m in registry.models_for_capabilities(required)]
        scanned = sorted(
            m.name for m in models
            if all(cap in m.capabilities for cap in required))
        assert indexed == scanned
