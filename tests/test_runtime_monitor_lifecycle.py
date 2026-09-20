from pathlib import Path

from forge.models.provider import ModelResult
from forge.models.runtime_monitor_service import RuntimeMonitorService


class Provider:
    model = "model-a"

    def list_models(self):
        return [self.model]

    def generate(self, prompt, **kwargs):
        return ModelResult("OK", self.model)


class Providers:
    def get(self, name):
        return Provider() if name == "fake" else None


class Registry:
    def snapshot(self):
        return [{"name": "model-a", "provider": "fake"}]


class Fabric:
    providers = Providers()
    registry = Registry()


def test_runtime_monitor_start_stop_and_persist(tmp_path):
    path = Path(tmp_path) / "runtime.json"
    service = RuntimeMonitorService(
        Fabric(), state_path=path, interval_seconds=60)

    service.tick(force=True, now=100.0)
    counts = service.snapshot()["counts"]
    # A successful model-list probe is discovery evidence only: the runtime
    # becomes CONFIGURED and is NOT promoted. Only an explicit real inference
    # probe may move it through VERIFIED to LIVE.
    assert counts["CONFIGURED"] == 1
    assert counts["LIVE"] == 0

    result = service.inference_check("fake", "model-a")
    assert result["ok"] is True
    assert result["state"] == "LIVE"
    assert service.snapshot()["counts"]["LIVE"] == 1

    service.start()
    assert service.running
    service.stop(wait=True)
    assert not service.running
    assert path.exists()

    restored = RuntimeMonitorService(
        Fabric(), state_path=path, interval_seconds=60)
    snapshot = restored.snapshot()
    assert snapshot["counts"]["LIVE"] == 1
    assert snapshot["runtimes"][0]["model_id"] == "model-a"
