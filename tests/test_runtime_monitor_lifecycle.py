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

    tick = service.tick(force=True, now=100.0)
    counts = service.snapshot()["counts"]
    # The model-list hit is discovery evidence only; the runtime is promoted
    # by the bounded real inference probe that follows it in the same tick.
    kinds = [item.get("kind") for item in tick["results"]]
    assert "inference" in kinds
    assert counts["LIVE"] == 1
    assert counts["CONFIGURED"] == 0
    assert service.registry.get("fake", "model-a").verification_id.startswith("inference:")

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
