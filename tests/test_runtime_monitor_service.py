from forge.models.configured_runtime import ConfiguredRuntime, ConfiguredRuntimeRegistry, RuntimeState
from forge.models.runtime_monitor import RuntimeMonitor
from forge.models.runtime_monitor_service import RuntimeMonitorService
from forge.models.runtime_verification import RuntimeProbeResult


class FakeProvider:
    def __init__(self, names):
        self.names = list(names)

    def list_models(self):
        return list(self.names)


class FakeProviders:
    def __init__(self, provider):
        self.provider = provider

    def get(self, name):
        return self.provider if name == "fake" else None

    def names(self):
        return ["fake"]

    def info(self, name):
        return type("Info", (), {"endpoint": "http://127.0.0.1", "local": True})()


class FakeModel:
    def __init__(self, name):
        self.name = name
        self.capabilities = ("text",)


class FakeRegistry:
    def snapshot(self):
        return [{"name": "model-a", "provider": "fake"}]

    def get(self, name):
        return FakeModel(name)


class FakeFabric:
    def __init__(self, names):
        self.providers = FakeProviders(FakeProvider(names))
        self.registry = FakeRegistry()


def test_runtime_monitor_recovers_unavailable_runtime():
    runtime = ConfiguredRuntime(provider="fake", model_id="model-a")
    runtime.set_configured()
    runtime.mark_unavailable("temporary failure")
    monitor = RuntimeMonitor()

    result = monitor.check(
        runtime,
        lambda p, m: RuntimeProbeResult(
            model_id=m, ok=True, status="healthy", capabilities=("text",)),
        now=100.0,
    )

    assert result.state == RuntimeState.LIVE.value
    assert runtime.routable


def test_service_uses_exact_model_list_and_persists(tmp_path):
    service = RuntimeMonitorService(
        FakeFabric(["model-a"]),
        state_path=tmp_path / "runtime.json",
        interval_seconds=5,
    )

    snapshot = service.tick(now=100.0, force=True)

    assert snapshot["live"] == 0
    assert snapshot["counts"][RuntimeState.CONFIGURED.value] == 1
    assert (tmp_path / "runtime.json").exists()
    assert snapshot["runtimes"][0]["model_id"] == "model-a"


def test_service_fails_closed_when_exact_model_missing(tmp_path):
    service = RuntimeMonitorService(
        FakeFabric(["other-model"]),
        state_path=tmp_path / "runtime.json",
        interval_seconds=5,
    )

    snapshot = service.tick(now=100.0, force=True)

    assert snapshot["live"] == 0
    assert snapshot["counts"][RuntimeState.UNAVAILABLE.value] == 1
    assert snapshot["runtimes"][0]["last_reason"] == "exact model is not available"


class InferenceProvider:
    model = "model-a"
    def list_models(self):
        return ["model-a"]
    def generate(self, prompt, *, context="", task=""):
        from forge.models.provider import ModelResult
        return ModelResult("OK", self.model)


class InferenceProviders(FakeProviders):
    def __init__(self):
        self.provider = InferenceProvider()


def test_service_explicit_inference_probe_promotes_exact_runtime(tmp_path):
    class Fabric(FakeFabric):
        def __init__(self):
            self.providers = InferenceProviders()
            self.registry = FakeRegistry()

    fabric = Fabric()
    service = RuntimeMonitorService(fabric, state_path=tmp_path / "runtime.json")
    service.tick(now=100.0, force=True)
    # The periodic check proves provider/model listing; this explicit endpoint
    # path proves that the provider can actually return an inference response.
    result = service.inference_check("fake", "model-a")
    assert result["ok"] is True
    assert result["state"] == RuntimeState.LIVE.value
    assert result["probe"]["reason"] == "real inference probe succeeded"

# -- regression: production id shapes, flapping, budgets, traffic evidence ------

from forge.models.fabric import ModelFabric  # noqa: E402
from forge.models.provider import ModelResult, ProviderInfo  # noqa: E402
from forge.models.registry import Model, ModelRegistry  # noqa: E402


class MultiModelProvider:
    """Hosted-style adapter: lists bare ids, serves any of them per request."""

    name = "hosted"

    def __init__(self, listed, *, fail=()):
        self.model = listed[0]
        self.listed = list(listed)
        self.fail = set(fail)
        self.calls = []

    def list_models(self):
        return list(self.listed)

    def generate(self, prompt, *, context="", task="", instructions="",
                 max_output_tokens=None, temperature=None, model=None):
        target = model or self.model
        self.calls.append((target, max_output_tokens))
        if target in self.fail:
            raise RuntimeError("hosted HTTP 404")
        return ModelResult("OK", target)


def _hosted_fabric(provider_name, provider, model_ids, *, prefixed=True):
    registry = ModelRegistry()
    fabric = ModelFabric(registry=registry)
    fabric.register_provider(provider_name, provider, ProviderInfo(
        name=provider_name, kind="remote", local=False, free=False))
    for model_id in model_ids:
        name = "%s/%s" % (provider_name, model_id) if prefixed else model_id
        fabric.register_model(Model(name=name, provider=provider_name,
                                    capabilities=("coding",), local=False, free=False,
                                    metadata={"runtime_verified": False}))
    return fabric, registry


def test_namespaced_registry_ids_match_bare_provider_inventory(tmp_path):
    """``anthropic/claude-x`` in the registry must match ``claude-x`` in the inventory."""
    provider = MultiModelProvider(["claude-x", "claude-y"])
    fabric, registry = _hosted_fabric("anthropic", provider, ["claude-x", "claude-y"])
    service = RuntimeMonitorService(fabric, state_path=tmp_path / "runtime.json")

    snapshot = service.tick(force=True, now=100.0)

    assert snapshot["counts"][RuntimeState.LIVE.value] == 2
    assert snapshot["counts"][RuntimeState.UNAVAILABLE.value] == 0
    assert registry.get("anthropic/claude-x").available is True
    assert registry.get("anthropic/claude-y").metadata["runtime_verified"] is True
    # The probe asked the provider for the exact bare id, with a tiny budget.
    assert ("claude-y", 8) in provider.calls
    providers = snapshot["providers"]["anthropic"]
    assert providers["discovered"] == 2 and providers["live"] == 2 and providers["configured"] == 2


def test_ollama_latest_tag_matches_untagged_registry_name(tmp_path):
    provider = MultiModelProvider(["llama3.2:latest"])
    fabric, registry = _hosted_fabric("ollama", provider, ["llama3.2"])
    service = RuntimeMonitorService(fabric, state_path=tmp_path / "runtime.json")

    snapshot = service.tick(force=True, now=100.0)

    assert snapshot["counts"][RuntimeState.LIVE.value] == 1
    assert registry.get("ollama/llama3.2").available is True


def test_verified_runtime_does_not_flap_on_stale_discovery_recheck(tmp_path):
    provider = MultiModelProvider(["m1"])
    fabric, registry = _hosted_fabric("hosted", provider, ["m1"])
    service = RuntimeMonitorService(fabric, state_path=tmp_path / "runtime.json",
                                    verification_ttl_seconds=300)
    service.tick(force=True, now=100.0)
    assert service.registry.get("hosted", "hosted/m1").state == RuntimeState.LIVE.value
    probes_after_first_tick = len(provider.calls)

    # Past the TTL the monitor re-checks the inventory; the model is still
    # listed, so it stays LIVE and routable without another paid probe.
    service.tick(force=True, now=100.0 + 301.0)

    runtime = service.registry.get("hosted", "hosted/m1")
    assert runtime.state == RuntimeState.LIVE.value
    assert runtime.last_checked == 401.0
    assert registry.get("hosted/m1").available is True
    assert registry.get("hosted/m1").metadata["runtime_verified"] is True
    assert len(provider.calls) == probes_after_first_tick


def test_inference_probes_are_bounded_per_tick_and_back_off_on_failure(tmp_path):
    ids = ["m%d" % i for i in range(5)]
    provider = MultiModelProvider(ids, fail={"m4"})
    fabric, registry = _hosted_fabric("hosted", provider, ids)
    service = RuntimeMonitorService(fabric, state_path=tmp_path / "runtime.json",
                                    inference_probe_budget=2, inference_retry_seconds=100)

    first = service.tick(force=True, now=100.0)
    assert first["counts"][RuntimeState.LIVE.value] == 2
    assert first["counts"][RuntimeState.CONFIGURED.value] == 3
    assert len(provider.calls) == 2

    service.tick(force=True, now=160.0)
    third = service.tick(force=True, now=220.0)
    assert third["counts"][RuntimeState.LIVE.value] == 4
    assert third["counts"][RuntimeState.UNAVAILABLE.value] == 1
    failed = service.registry.get("hosted", "hosted/m4")
    assert "hosted HTTP 404" in failed.last_reason
    assert registry.get("hosted/m4").available is False
    calls_after_failure = len(provider.calls)

    # Not retried before the backoff window closes, retried after it.
    service.tick(force=True, now=250.0)
    assert len(provider.calls) == calls_after_failure
    provider.fail.clear()
    service.tick(force=True, now=400.0)
    assert service.registry.get("hosted", "hosted/m4").state == RuntimeState.LIVE.value
    assert registry.get("hosted/m4").available is True


def test_real_generation_success_is_verification_evidence(tmp_path):
    provider = MultiModelProvider(["m1"])
    fabric, registry = _hosted_fabric("hosted", provider, ["m1"])
    service = RuntimeMonitorService(fabric, state_path=tmp_path / "runtime.json",
                                    inference_probes=False)
    service.tick(force=True, now=100.0)
    assert service.registry.get("hosted", "hosted/m1").state == RuntimeState.CONFIGURED.value

    from forge.models.request import ModelRequest
    response = fabric.generate(ModelRequest(prompt="hello", capability="coding"))
    assert response.success and response.model == "hosted/m1"
    assert provider.calls[-1][0] == "m1"
    assert registry.get("hosted/m1").metadata["runtime_verified"] is True
    assert registry.get("hosted/m1").metadata["verification_source"] == "generation"

    tick = service.tick(force=True, now=160.0)
    assert service.registry.get("hosted", "hosted/m1").state == RuntimeState.LIVE.value
    assert any(item.get("kind") == "traffic" for item in tick["results"])


def test_discovery_transport_failure_is_inconclusive(tmp_path):
    class Flaky(MultiModelProvider):
        def list_models(self):
            raise RuntimeError("HTTP 503")

    provider = Flaky(["m1"])
    fabric, registry = _hosted_fabric("hosted", provider, ["m1"])
    service = RuntimeMonitorService(fabric, state_path=tmp_path / "runtime.json")

    snapshot = service.tick(force=True, now=100.0)

    # Discovery could not run; the bounded inference probe still decides.
    assert snapshot["providers"]["hosted"]["discovered"] is None
    assert "discovery failed" in snapshot["providers"]["hosted"]["discovery_error"]
    assert service.registry.get("hosted", "hosted/m1").state == RuntimeState.LIVE.value
    assert registry.get("hosted/m1").available is True
