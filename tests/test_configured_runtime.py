from forge.models.configured_runtime import (
    ConfiguredRuntime,
    ConfiguredRuntimeRegistry,
    RuntimeState,
)


def test_configured_does_not_become_live_without_verification():
    runtime = ConfiguredRuntime(provider="openai", model_id="example")
    runtime.set_configured(valid=True)
    assert runtime.state == RuntimeState.CONFIGURED.value
    assert not runtime.routable

    runtime.mark_verified(verification_id="probe-1", capabilities=("reasoning",))
    assert runtime.state == RuntimeState.VERIFIED.value
    assert not runtime.routable

    runtime.activate()
    assert runtime.state == RuntimeState.LIVE.value
    assert runtime.routable


def test_invalid_and_unavailable_are_not_routable():
    runtime = ConfiguredRuntime(provider="provider", model_id="model")
    runtime.set_configured(valid=False, reason="bad endpoint")
    assert runtime.state == RuntimeState.INVALID_CONFIGURATION.value
    assert not runtime.routable

    runtime.set_configured(valid=True)
    runtime.mark_unavailable("connection refused")
    assert runtime.state == RuntimeState.UNAVAILABLE.value
    assert not runtime.routable


def test_verified_cannot_skip_configured_state():
    runtime = ConfiguredRuntime(provider="provider", model_id="model")
    try:
        runtime.mark_verified(verification_id="probe")
    except RuntimeError:
        pass
    else:
        raise AssertionError("verification must require CONFIGURED state")


def test_registry_snapshot_excludes_arbitrary_metadata_and_counts_live():
    registry = ConfiguredRuntimeRegistry()
    runtime = ConfiguredRuntime(
        provider="openai",
        model_id="model",
        endpoint="https://user:secret@example.invalid/v1",
        metadata={"secret": "must-not-leak"},
    )
    runtime.set_configured()
    runtime.mark_verified(verification_id="probe")
    runtime.activate()
    registry.register(runtime)

    snapshot = registry.snapshot()[0]
    assert snapshot["endpoint"] == "https://example.invalid/v1"
    assert "metadata" not in snapshot
    assert registry.counts()[RuntimeState.LIVE.value] == 1
    assert len(registry.live()) == 1
