from pathlib import Path

from forge.models.runtime_monitor_service import RuntimeMonitorService


class Provider:
    def list_models(self):
        return ["model-a"]


class Providers:
    def get(self, name):
        return Provider() if name == "fake" else None


class Fabric:
    providers = Providers()


def test_runtime_monitor_start_stop_and_persist(tmp_path):
    path = Path(tmp_path) / "runtime.json"
    service = RuntimeMonitorService(
        Fabric(), state_path=path, interval_seconds=60)

    service.tick(force=True, now=100.0)
    assert service.snapshot()["counts"]["CONFIGURED"] == 1
    service.start()
    assert service.running
    service.stop(wait=True)
    assert not service.running
    assert path.exists()

    restored = RuntimeMonitorService(
        Fabric(), state_path=path, interval_seconds=60)
    snapshot = restored.snapshot()
    assert snapshot["runtimes"][0]["model_id"] == "model-a"
