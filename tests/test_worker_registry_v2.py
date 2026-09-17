from forge.workers.registry import WorkerRegistry


def test_registry_selects_live_capable_worker_and_balances_load():
    registry = WorkerRegistry(heartbeat_ttl=60)
    slow = registry.register(name="cpu", capabilities=("python",), cpu_threads=4, ram_mb=4096)
    gpu = registry.register(name="gpu", capabilities=("python", "cuda"), cpu_threads=8, ram_mb=16384, gpu=True)
    assert registry.select(required_capabilities=("cuda",), require_gpu=True).worker_id == gpu.worker_id
    assert registry.begin_job(gpu.worker_id)
    assert registry.select(required_capabilities=("python",)).worker_id == slow.worker_id


def test_stale_worker_is_not_selected_and_reap_preserves_metadata():
    registry = WorkerRegistry(heartbeat_ttl=10)
    worker = registry.register(name="stale", capabilities=("python",))
    worker.last_heartbeat -= 100
    assert registry.select(required_capabilities=("python",)) is None
    assert registry.reap() == 1
    snapshot = registry.snapshot()
    assert snapshot["worker_count"] == 1
    assert snapshot["live_count"] == 0
    assert snapshot["workers"][0]["revoked"] is True


def test_revoked_worker_cannot_heartbeat_or_start_job():
    registry = WorkerRegistry()
    worker = registry.register(name="revoked", capabilities=("python",))
    assert registry.revoke(worker.worker_id)
    assert registry.heartbeat(worker.worker_id) is False
    assert registry.begin_job(worker.worker_id) is False


def test_resource_and_platform_constraints_fail_closed():
    registry = WorkerRegistry()
    registry.register(name="linux", capabilities=("python",), cpu_threads=2, ram_mb=2048, platform="linux")
    assert registry.select(min_ram_mb=4096) is None
    assert registry.select(min_cpu_threads=4) is None
    assert registry.select(platform="windows") is None
