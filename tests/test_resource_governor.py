"""Session 10 (D): unified resource governor + G560 thin-client profile.

The governor is the one canonical budget surface (CPU/RAM/disk/network/
concurrency/model memory/cost/time). The g560 profile encodes the 2 GB
thin client: never an inference machine, bounded concurrency, heavy
work offloaded to a server. Everything fails closed and reports
honestly.
"""
from __future__ import annotations

import struct
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from forge.core.resource_governor import (  # noqa: E402
    ResourceGovernor,
    ResourceLimitExceeded,
    ResourceProfile,
    ResourceBudget,
    detect_profile,
    g560_profile,
    profile_from_name,
    select_profile,
    system_resources,
)
from forge.runtime.model_runtime import (  # noqa: E402
    NativeBackend,
    ModelRuntime,
    RuntimeCapacityError,
    RuntimeConfig,
)


# -- profiles -----------------------------------------------------------------


def test_g560_profile_is_restrictive():
    profile = g560_profile()
    assert profile.name == "g560"
    assert profile.budget.max_workers == 2
    assert profile.budget.model_loading_allowed is False
    assert profile.budget.model_memory_mb == 0
    assert profile.budget.network_policy == "server-only"


def test_default_profile_allows_model_loading_within_budget():
    profile = select_profile("default")
    assert profile.name == "default"
    assert profile.budget.model_loading_allowed is True
    # default has no explicit model memory bound (0 = unbounded)
    gov = ResourceGovernor(profile)
    ok, _ = gov.check_model_load(10 * 1024 * 1024)
    assert ok


def test_unknown_profile_fails_closed_to_restrictive():
    profile = profile_from_name("totally-unknown")
    assert profile.name.startswith("unknown:")
    # Fails closed: same restrictive bounds as g560
    assert profile.budget.model_loading_allowed is False
    assert profile.budget.max_workers == 2


def test_select_profile_explicit_wins_over_env(monkeypatch):
    monkeypatch.setenv("FORGE_RESOURCE_PROFILE", "default")
    # Explicit g560 must win over the env's default.
    assert select_profile("g560").name == "g560"
    # No explicit: env wins.
    assert select_profile("").name == "default"


# -- model loading -------------------------------------------------------------


def test_g560_refuses_any_local_model_load():
    gov = ResourceGovernor(g560_profile())
    for size in (0, 1, 1024, 10 * 1024 * 1024):
        ok, reason = gov.check_model_load(size)
        assert ok is False
        assert "denied" in reason.lower()


def test_model_load_over_memory_budget_refused():
    profile = ResourceProfile(
        name="capped",
        budget=ResourceBudget(model_loading_allowed=True,
                              model_memory_mb=1),
        rationale="test")
    gov = ResourceGovernor(profile)
    ok, _ = gov.check_model_load(1024 * 1024)  # exactly 1 MB: ok
    assert ok
    ok, reason = gov.check_model_load(1024 * 1024 + 1)  # over 1 MB
    assert ok is False
    assert "exceeds" in reason.lower()


# -- concurrency ----------------------------------------------------------------


def test_concurrency_gate_is_bounded():
    gov = ResourceGovernor(g560_profile())  # max_workers = 2
    seen = []
    lock = threading.Lock()

    def worker():
        with gov.concurrency():
            with lock:
                seen.append(len(seen))
            time.sleep(0.05)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    # All 5 completed, but never more than 2 held a slot at once.
    assert len(seen) == 5
    snap = gov.snapshot()
    assert snap["usage"]["concurrency_capacity"] == 2
    assert snap["usage"]["concurrency_peak"] <= 2


def test_bounded_semaphore_rejects_over_acquire():
    gov = ResourceGovernor(g560_profile())  # 2 slots
    cm1 = gov.concurrency()
    cm2 = gov.concurrency()
    cm1.__enter__()
    cm2.__enter__()
    try:
        # The gate is bounded: a third non-blocking acquire simply cannot
        # get a slot (returns False) — over-booking is impossible by
        # construction, not by convention.
        assert gov._semaphore.acquire(blocking=False) is False
    finally:
        cm1.__exit__(None, None, None)
        cm2.__exit__(None, None, None)


def test_clamp_workers_never_exceeds_profile():
    gov = ResourceGovernor(g560_profile())
    assert gov.clamp_workers(8) == 2
    assert gov.clamp_workers(1) == 1
    default_gov = ResourceGovernor(select_profile("default"))
    assert default_gov.clamp_workers(100) <= default_gov.clamp_workers(4)


# -- cost and scratch -----------------------------------------------------------


def test_cost_budget_is_enforced_and_recorded():
    profile = ResourceProfile(
        name="costy", budget=ResourceBudget(max_cost_usd=1.0),
        rationale="test")
    gov = ResourceGovernor(profile)
    gov.spend_cost(0.6)
    with pytest.raises(ResourceLimitExceeded) as exc:
        gov.spend_cost(0.5)  # would exceed $1.00
    assert exc.value.code == "COST_BUDGET"
    # The refusal is visible in the snapshot (denials bounded to last 20).
    snap = gov.snapshot()
    assert snap["usage"]["cost_usd"] == pytest.approx(0.6)
    assert any(d["code"] == "COST_BUDGET" for d in snap["usage"]["denials"])


def test_scratch_budget_is_enforced_and_releasable():
    profile = ResourceProfile(
        name="scratchy", budget=ResourceBudget(scratch_disk_mb=1),
        rationale="test")
    gov = ResourceGovernor(profile)
    gov.scratch_reserve(1024 * 1024)  # exactly the 1 MB budget
    with pytest.raises(ResourceLimitExceeded) as exc:
        gov.scratch_reserve(1)
    assert exc.value.code == "SCRATCH_BUDGET"
    gov.scratch_release(1024 * 1024)
    gov.scratch_reserve(512 * 1024)  # now fits
    assert gov.snapshot()["usage"]["scratch_bytes"] == 512 * 1024


# -- wall-clock -----------------------------------------------------------------


def test_wall_clock_deadline_enforced():
    profile = ResourceProfile(
        name="slow", budget=ResourceBudget(max_task_wall_seconds=0.1),
        rationale="test")
    gov = ResourceGovernor(profile)
    with pytest.raises(ResourceLimitExceeded) as exc:
        with gov.deadline():
            time.sleep(0.3)
    assert exc.value.code == "WALL_TIME"


def test_wall_clock_passes_within_bound():
    gov = ResourceGovernor(select_profile("default"))
    with gov.deadline(seconds=1.0):
        time.sleep(0.01)
    # No denial recorded.
    assert not any(d["code"] == "WALL_TIME"
                   for d in gov.snapshot()["usage"]["denials"])


# -- honest machine snapshot ----------------------------------------------------


def test_system_resources_is_honest():
    res = system_resources()
    assert "cpu_count" in res
    assert "memory_measured" in res
    if res["memory_measured"]:
        assert res["memory_total_mb"] is not None
        assert res["memory_total_mb"] > 0
    # The measured flag is consistent with the value.
    assert res["memory_measured"] == (res["memory_total_mb"] is not None)


def test_detect_profile_low_memory_is_g560():
    profile = detect_profile({"memory_total_mb": 2048, "cpu_count": 4})
    assert profile.name == "g560"
    profile = detect_profile({"memory_total_mb": 8192, "cpu_count": 8})
    assert profile.name == "default"
    # Unmeasurable memory is advisory: default, never a capability grant.
    profile = detect_profile({"memory_total_mb": None, "cpu_count": 4})
    assert profile.name == "default"


# -- runtime integration --------------------------------------------------------


def _write_gguf(path: Path) -> Path:
    path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 4, 5) + b"\0" * 32)
    return path


def test_runtime_refuses_local_load_under_g560(tmp_path):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    _write_gguf(model_dir / "tiny.gguf")
    backend = NativeBackend(model_dirs=(str(model_dir),))
    config = RuntimeConfig(model_dirs=(str(model_dir),))
    runtime = ModelRuntime(config, backends=[backend],
                           governor=ResourceGovernor(g560_profile()))
    runtime.discover("native")
    calls = []
    original = backend.load_model

    def counting(model, token=None):
        calls.append(model.model_id)
        return original(model, token)

    backend.load_model = counting
    with pytest.raises(RuntimeCapacityError) as exc:
        runtime.load("native:tiny.gguf")
    # Refused by the governor BEFORE any backend load was attempted.
    assert "governor" in str(exc.value).lower()
    assert calls == []
    assert not any(m.loaded for m in runtime.models("native"))


def test_runtime_allows_load_under_default_profile(tmp_path):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    _write_gguf(model_dir / "tiny.gguf")
    backend = NativeBackend(model_dirs=(str(model_dir),))
    config = RuntimeConfig(model_dirs=(str(model_dir),))
    runtime = ModelRuntime(config, backends=[backend],
                           governor=ResourceGovernor(
                               select_profile("default")))
    runtime.discover("native")
    model = runtime.load("native:tiny.gguf")
    assert model.loaded is True


# -- server integration ---------------------------------------------------------


def test_server_clamps_workers_to_profile(tmp_path):
    sys.path.insert(0, str(Path(__file__).parent))
    from helpers_server import make_server  # noqa: E402

    server = make_server(tmp_path, max_workers=8, start=False,
                         resource_profile="g560")
    # g560 caps concurrency at 2, regardless of the requested 8.
    assert server.pool.max_workers == 2
    assert server.status()["resource_profile"]["name"] == "g560"
