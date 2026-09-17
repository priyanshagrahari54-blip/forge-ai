from forge.models.configured_runtime import ConfiguredRuntime, RuntimeState
from forge.models.runtime_monitor import RuntimeMonitor
from forge.models.runtime_verification import RuntimeProbeResult


def probe_ok(provider, model_id):
    return RuntimeProbeResult(
        model_id=model_id, ok=True, latency_ms=12.0,
        status="HEALTHY", capabilities=("coding",),
    )


def probe_bad(provider, model_id):
    return RuntimeProbeResult(
        model_id=model_id, ok=False, latency_ms=20.0,
        status="UNHEALTHY", reason="connection refused",
    )


def test_configured_probe_promotes_to_live():
    runtime = ConfiguredRuntime("ollama", "llama3.2")
    runtime.set_configured()
    result = RuntimeMonitor().check(runtime, probe_ok, now=100.0)
    assert result.state == RuntimeState.LIVE.value
    assert runtime.routable
    assert runtime.last_checked == 100.0
    assert runtime.verification_id


def test_failed_probe_removes_routability():
    runtime = ConfiguredRuntime("ollama", "llama3.2")
    runtime.set_configured()
    RuntimeMonitor().check(runtime, probe_ok, now=100.0)
    result = RuntimeMonitor().check(runtime, probe_bad, now=110.0)
    assert result.state == RuntimeState.UNAVAILABLE.value
    assert not runtime.routable


def test_wrong_identity_is_rejected():
    runtime = ConfiguredRuntime("provider", "model-a")
    runtime.set_configured()

    def wrong(provider, model_id):
        return RuntimeProbeResult("model-b", True, 1.0, "HEALTHY")

    try:
        RuntimeMonitor().check(runtime, wrong)
    except ValueError:
        pass
    else:
        raise AssertionError("identity mismatch must be rejected")


def test_stale_live_runtime_is_detected():
    runtime = ConfiguredRuntime("provider", "model")
    runtime.set_configured()
    RuntimeMonitor().check(runtime, probe_ok, now=100.0)
    monitor = RuntimeMonitor(verification_ttl_seconds=30)
    assert not monitor.stale(runtime, now=120.0)
    assert monitor.stale(runtime, now=131.0)
