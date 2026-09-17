from pathlib import Path

from forge.api.app import create_app
from forge.control.control_plane import ControlConfig, ControlPlane
from forge.workers.persistence import WorkerStore
from forge.workers.registry import WorkerRegistry


def test_worker_store_round_trip_requires_fresh_heartbeat(tmp_path):
    registry = WorkerRegistry(heartbeat_ttl=60)
    worker = registry.register(
        name="remote-a", capabilities=("coding",), cpu_threads=4,
        ram_mb=8192, platform="linux", endpoint="ssh://worker-a")
    store = WorkerStore(str(tmp_path / "workers.db"))
    store.save(worker)

    restored = WorkerRegistry(heartbeat_ttl=60)
    assert store.restore(restored) == 1
    record = restored.get(worker.worker_id)
    assert record is not None
    assert not record.live()
    assert restored.select(required_capabilities=("coding",)) is None
    assert restored.heartbeat(worker.worker_id)
    assert restored.select(required_capabilities=("coding",)) is not None


def test_app_initializes_worker_persistence(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    plane = ControlPlane(ControlConfig(db_path=str(tmp_path / "cockpit.db"),
                                       projects={"p": str(project)}))
    app = create_app(plane)
    assert app.state.plane.worker_registry is not None
    assert any(getattr(route, "path", "") == "/api/v1/readiness"
               for route in app.routes)
    plane.stop(wait=False)
