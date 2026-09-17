"""Cross-cutting integration contract tests for the final Forge stack."""
from __future__ import annotations

from forge.client.thin_client import select_profile
from forge.runtime.system import runtime_snapshot
from forge.workers.admission import WorkerRequirements
from forge.workers.execution_gateway import ExecutionGateway
from forge.workers.registry import WorkerRegistry


def test_g560_profile_is_thin_and_remote_preferred():
    profile = select_profile(ram_mb=2048, cpu_threads=2)
    assert profile.name == "lenovo-g560-2gb"
    assert profile.remote_execution_preferred is True
    assert profile.local_compute_enabled is False
    assert profile.heavy_media_local is False


def test_remote_execution_requires_live_worker_and_releases_capacity():
    registry = WorkerRegistry(heartbeat_ttl=60)
    worker = registry.register(
        name="integration-worker", capabilities=("python", "coding"),
        cpu_threads=8, ram_mb=16384, gpu=True, platform="linux",
        endpoint="ssh://worker")
    gateway = ExecutionGateway(registry)
    receipt = gateway.remote(
        WorkerRequirements(capabilities=("python",), min_ram_mb=4096,
                           min_cpu_threads=2, require_gpu=True,
                           platform="linux", remote_required=True,
                           retryable=True, idempotent=True),
        lambda selected: {"worker": selected.worker_id, "ok": True})
    assert receipt.ok is True
    assert receipt.worker_id == worker.worker_id
    assert registry.get(worker.worker_id).active_jobs == 0


def test_runtime_snapshot_preserves_truth_boundaries():
    registry = WorkerRegistry()
    snapshot = runtime_snapshot(worker_registry=registry)
    assert snapshot["schema_version"] == 1
    assert snapshot["execution"]["remote_requires_admission"] is True
    assert snapshot["execution"]["external_provider_state_is_runtime_verified"] is True
    assert snapshot["client_profile"]["name"] == "lenovo-g560-2gb"
