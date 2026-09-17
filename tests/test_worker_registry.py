from __future__ import annotations

import time

from forge.compute.worker_registry import ComputeWorker, WorkerCapability, WorkerRegistry


def test_select_requires_fresh_capable_worker():
    registry = WorkerRegistry(heartbeat_ttl=10.0)
    registry.register(ComputeWorker(
        "gpu-1",
        WorkerCapability(cpu_threads=8, memory_mb=16384, gpu=True,
                          platforms=("linux",), labels=("cuda", "ai")),
    ))
    registry.register(ComputeWorker(
        "small-1",
        WorkerCapability(cpu_threads=2, memory_mb=4096, gpu=False,
                          platforms=("linux",), labels=("general",)),
    ))

    selected = registry.select(min_memory_mb=8000, gpu=True,
                               platform="linux", labels=("ai",))
    assert selected is not None
    assert selected.worker_id == "gpu-1"


def test_stale_worker_is_not_selected():
    registry = WorkerRegistry(heartbeat_ttl=5.0)
    worker = ComputeWorker(
        "old-1", WorkerCapability(cpu_threads=16, memory_mb=32768),
        last_heartbeat=time.time() - 100,
    )
    registry._workers[worker.worker_id] = worker

    assert registry.select(min_memory_mb=1) is None
    stale = registry.mark_stale()
    assert [item.worker_id for item in stale] == ["old-1"]
    assert registry.get("old-1").state == "STALE"


def test_heartbeat_restores_online_worker():
    registry = WorkerRegistry(heartbeat_ttl=5.0)
    worker = ComputeWorker("worker-1", WorkerCapability(cpu_threads=4, memory_mb=4096))
    registry.register(worker)
    worker.last_heartbeat = time.time() - 100
    registry.mark_stale()

    assert registry.heartbeat("worker-1") is worker
    assert worker.state == "ONLINE"
    assert registry.select(min_cpu_threads=4) is worker


def test_revoked_worker_cannot_return_with_heartbeat():
    registry = WorkerRegistry()
    worker = registry.register(ComputeWorker("worker-1", WorkerCapability()))
    assert registry.revoke(worker.worker_id) is True
    assert registry.heartbeat(worker.worker_id) is None
    assert registry.select() is None
