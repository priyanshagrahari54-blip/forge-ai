from pathlib import Path

import pytest

from forge.security.sandbox_policy import SandboxPolicy
from forge.workers.admission import AdmissionError, WorkerAdmission, WorkerRequirements
from forge.workers.registry import WorkerRegistry


def test_admission_requires_capability_and_reserves_worker():
    registry = WorkerRegistry(heartbeat_ttl=60)
    worker = registry.register(
        name="gpu-builder",
        capabilities=("python", "cuda", "docker"),
        cpu_threads=16,
        ram_mb=32768,
        gpu=True,
        platform="linux",
    )
    admission = WorkerAdmission(registry)
    result = admission.admit(WorkerRequirements(
        capabilities=("cuda", "python"),
        min_ram_mb=16000,
        min_cpu_threads=8,
        require_gpu=True,
        platform="linux",
        remote_required=True,
    ))
    assert result.worker_id == worker.worker_id
    assert registry.get(worker.worker_id).active_jobs == 1
    admission.release(result)
    assert registry.get(worker.worker_id).active_jobs == 0


def test_admission_fails_closed_for_missing_worker():
    admission = WorkerAdmission(WorkerRegistry())
    with pytest.raises(AdmissionError):
        admission.admit(WorkerRequirements(
            capabilities=("cuda",), require_gpu=True, remote_required=True
        ))


def test_stale_worker_is_not_admitted():
    registry = WorkerRegistry(heartbeat_ttl=10)
    worker = registry.register(name="old", capabilities=("python",))
    admission = WorkerAdmission(registry)
    with pytest.raises(AdmissionError):
        admission.admit(
            WorkerRequirements(capabilities=("python",), remote_required=True),
            now=worker.last_heartbeat + 11,
        )


def test_expired_lease_retry_requires_idempotency():
    admission = WorkerAdmission(WorkerRegistry())
    assert not admission.can_retry_after_expiry(
        WorkerRequirements(remote_required=True, retryable=True, idempotent=False)
    )
    assert admission.can_retry_after_expiry(
        WorkerRequirements(remote_required=True, retryable=True, idempotent=True)
    )


def test_sandbox_rejects_path_escape_and_unapproved_write(tmp_path: Path):
    policy = SandboxPolicy(root=tmp_path, writable_paths=("src",))
    assert policy.allows_path(tmp_path / "src" / "main.py", write=True)
    assert not policy.allows_path(tmp_path / "secrets.txt", write=True)
    assert not policy.allows_path(tmp_path.parent / "outside.txt", write=False)


def test_sandbox_is_explicitly_not_an_isolation_claim(tmp_path: Path):
    policy = SandboxPolicy(root=tmp_path)
    assert "must enforce" in policy.to_dict()["isolation_note"]
