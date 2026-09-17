from forge.workers.admission import WorkerRequirements
from forge.workers.execution_gateway import ExecutionGateway
from forge.workers.registry import WorkerRegistry
from forge.client.thin_client import G560_PROFILE, select_profile


def test_remote_gateway_admits_and_releases_worker():
    registry = WorkerRegistry()
    worker = registry.register(
        name="gpu-1", capabilities=("python", "gpu"), cpu_threads=8,
        ram_mb=16384, gpu=True, platform="linux", endpoint="ssh://worker")
    gateway = ExecutionGateway(registry)
    receipt = gateway.remote(
        WorkerRequirements(capabilities=("python",), min_ram_mb=4096,
                           require_gpu=True, platform="linux",
                           remote_required=True),
        lambda admission: {"status": "succeeded", "backend": "test-transport",
                           "worker": admission.worker_id})
    assert receipt.status == "succeeded"
    assert receipt.worker_id == worker.worker_id
    assert registry.get(worker.worker_id).active_jobs == 0


def test_remote_gateway_fails_closed_without_worker():
    gateway = ExecutionGateway(WorkerRegistry())
    receipt = gateway.remote(
        WorkerRequirements(capabilities=("cuda",), min_ram_mb=64000,
                           require_gpu=True, remote_required=True),
        lambda _: {"status": "succeeded"})
    assert receipt.status == "refused"
    assert receipt.worker_id == ""


def test_thin_client_profile_is_conservative():
    assert select_profile(ram_mb=2048, cpu_threads=2) == G560_PROFILE
    assert G560_PROFILE.heavy_media_local is False
    assert G560_PROFILE.remote_execution_preferred is True
