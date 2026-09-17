from pathlib import Path

from forge.models.discovery_pipeline import RealModelDiscoveryPipeline
from forge.models.oss_registry import OSSModel
from forge.models.registry import ModelRegistry
from forge.models.runtime_verification import RuntimeProbeResult


def _inventory() -> list[OSSModel]:
    return [
        OSSModel("org/model-a", "org", "huggingface", tags=("coding",)),
        OSSModel("org/model-b", "org", "huggingface", tags=("reasoning",)),
    ]


def test_register_does_not_make_discovered_models_live(tmp_path: Path):
    registry = ModelRegistry()
    pipeline = RealModelDiscoveryPipeline(registry, state_path=tmp_path / "models.json", target=2)
    pipeline.inventory = {item.model_id: item for item in _inventory()}

    added = pipeline.register()
    assert added == 2
    assert pipeline.snapshot().discovered == 2
    assert pipeline.snapshot().registered == 2
    assert pipeline.snapshot().verified == 0
    assert pipeline.snapshot().live == 0
    assert not registry.get("org/model-a").available
    assert not registry.get("org/model-a").metadata["runtime_verified"]


def test_successful_exact_probe_is_the_only_activation_path(tmp_path: Path):
    registry = ModelRegistry()
    pipeline = RealModelDiscoveryPipeline(registry, state_path=tmp_path / "models.json", target=1)
    item = _inventory()[0]
    pipeline.inventory = {item.model_id: item}
    pipeline.register()

    result = pipeline.verify(
        item.model_id,
        lambda model: RuntimeProbeResult(
            model_id=model.name,
            ok=True,
            latency_ms=12.5,
            status="HEALTHY",
            reason="verified test adapter",
            capabilities=("coding",),
            context_window=8192,
            max_output_tokens=2048,
        ),
    )

    assert result.ok
    snap = pipeline.snapshot()
    assert snap.verified == 1
    assert snap.live == 1
    assert registry.get(item.model_id).metadata["runtime_verified"] is True


def test_unknown_model_cannot_be_verified(tmp_path: Path):
    pipeline = RealModelDiscoveryPipeline(ModelRegistry(), state_path=tmp_path / "models.json")
    try:
        pipeline.verify("not/discovered", lambda model: RuntimeProbeResult(
            model_id=model.name, ok=True, latency_ms=1.0, status="HEALTHY", reason="x"
        ))
    except KeyError as exc:
        assert "not/discovered" in str(exc)
    else:
        raise AssertionError("unknown model was accepted")


def test_inventory_persists_and_reloads(tmp_path: Path):
    state = tmp_path / "models.json"
    first = RealModelDiscoveryPipeline(ModelRegistry(), state_path=state, target=2)
    first.inventory = {item.model_id: item for item in _inventory()}
    first._save()

    second = RealModelDiscoveryPipeline(ModelRegistry(), state_path=state, target=2)
    assert set(second.inventory) == {"org/model-a", "org/model-b"}
    assert second.snapshot().meets_target
