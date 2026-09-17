from forge.models.registry import Model, ModelRegistry
from forge.models.runtime_verification import RuntimeProbeResult, apply_probe_result, verify_model


def test_successful_probe_activates_exact_model():
    registry = ModelRegistry([Model("org/model", "huggingface", available=False)])
    result = apply_probe_result(
        registry,
        RuntimeProbeResult(
            "org/model", True, 42.5, "healthy",
            capabilities=("coding", "reasoning"), context_window=32768,
        ),
    )
    assert result.available is True
    assert result.metadata["runtime_verified"] is True
    assert result.capabilities == ("coding", "reasoning")
    assert result.context_window == 32768
    assert result.latency_ms == 42.5


def test_failed_probe_blocks_model():
    registry = ModelRegistry([Model("org/model", "huggingface", available=True)])
    result = apply_probe_result(
        registry,
        RuntimeProbeResult("org/model", False, 1000.0, "unhealthy", "timeout"),
    )
    assert result.available is False
    assert result.health.status == "unhealthy"
    assert result.metadata["runtime_verified"] is False
    assert result.health.last_error == "timeout"


def test_verify_catches_provider_probe_errors():
    registry = ModelRegistry([Model("org/model", "huggingface", available=False)])
    result = verify_model(registry, "org/model", lambda _: (_ for _ in ()).throw(RuntimeError("offline")))
    assert result.ok is False
    assert result.model_id == "org/model"
    assert registry.get("org/model").available is False


def test_probe_cannot_change_model_identity():
    registry = ModelRegistry([Model("org/model", "huggingface", available=False)])
    try:
        verify_model(
            registry,
            "org/model",
            lambda _: RuntimeProbeResult("other/model", True, 1.0, "healthy"),
        )
    except ValueError as exc:
        assert "different model identity" in str(exc)
    else:
        raise AssertionError("identity mismatch must be rejected")
