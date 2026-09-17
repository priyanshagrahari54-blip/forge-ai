"""F: transactional resource reservation regression tests."""
from __future__ import annotations

import threading
import time

import pytest

from forge.core.resource_governor import (
    ResourceBudget,
    ResourceGovernor,
    ResourceLimitExceeded,
    ResourceProfile,
)
from forge.core.resource_reservation import reserve_resources


def governor(workers=1, scratch_mb=1, cost_usd=0.0):
    profile = ResourceProfile(
        name="test",
        budget=ResourceBudget(
            max_workers=workers,
            scratch_disk_mb=scratch_mb,
            max_cost_usd=cost_usd,
            max_task_wall_seconds=10.0,
        ),
    )
    return ResourceGovernor(profile, resources={"test": True})


def test_reservation_releases_scratch_and_worker_on_success():
    gov = governor()
    with reserve_resources(gov, scratch_bytes=128) as reservation:
        assert gov.snapshot()["usage"]["concurrency_now"] == 1
        assert gov.snapshot()["usage"]["scratch_bytes"] == 128
        reservation.settle_cost(0.25)
    usage = gov.snapshot()["usage"]
    assert usage["concurrency_now"] == 0
    assert usage["scratch_bytes"] == 0
    assert usage["cost_usd"] == 0.25


def test_reservation_releases_everything_when_execution_raises():
    gov = governor()
    with pytest.raises(RuntimeError):
        with reserve_resources(gov, scratch_bytes=256):
            raise RuntimeError("worker failed")
    usage = gov.snapshot()["usage"]
    assert usage["concurrency_now"] == 0
    assert usage["scratch_bytes"] == 0


def test_failed_scratch_reservation_does_not_consume_worker_slot():
    gov = governor(scratch_mb=1)
    with pytest.raises(ResourceLimitExceeded) as exc:
        with reserve_resources(gov, scratch_bytes=2 * 1024 * 1024):
            pass
    assert exc.value.code == "SCRATCH_BUDGET"
    usage = gov.snapshot()["usage"]
    assert usage["concurrency_now"] == 0
    assert usage["scratch_bytes"] == 0


def test_cost_is_settled_once_and_budget_is_enforced():
    gov = governor(cost_usd=1.0)
    with reserve_resources(gov, cost_usd=0.75) as reservation:
        reservation.settle_cost()
        with pytest.raises(RuntimeError):
            reservation.settle_cost()
    assert gov.snapshot()["usage"]["cost_usd"] == 0.75

    with reserve_resources(gov, cost_usd=0.5) as reservation:
        with pytest.raises(ResourceLimitExceeded) as exc:
            reservation.settle_cost()
        assert exc.value.code == "COST_BUDGET"
    assert gov.snapshot()["usage"]["concurrency_now"] == 0
    assert gov.snapshot()["usage"]["scratch_bytes"] == 0


def test_only_governor_capacity_can_execute_concurrently():
    gov = governor(workers=1)
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()

    def first():
        with reserve_resources(gov):
            entered.set()
            release.wait(2)

    def second():
        with reserve_resources(gov):
            second_entered.set()

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    assert entered.wait(1)
    t2.start()
    time.sleep(0.05)
    assert not second_entered.is_set()
    release.set()
    t1.join(1)
    t2.join(1)
    assert second_entered.is_set()
    assert gov.snapshot()["usage"]["concurrency_now"] == 0
