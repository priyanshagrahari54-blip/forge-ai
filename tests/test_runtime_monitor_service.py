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

    assert snapshot["live"] == 1
    assert snapshot["counts"][RuntimeState.LIVE.value] == 1
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