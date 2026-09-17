from forge.agents.fleet import build_fleet, fleet_snapshot
from forge.orchestration.failover import FailoverPool


def test_default_fleet_has_at_least_1000_logical_slots():
    fleet = build_fleet(1000)
    assert len(fleet) == 1000
    assert len({slot.name for slot in fleet}) == 1000


def test_fleet_snapshot_is_explicit_about_execution_model():
    snapshot = fleet_snapshot()
    assert snapshot["count"] >= 1000
    assert snapshot["mode"] == "logical-routable-fleet"
    assert snapshot["execution"] == "delegated-to-configured-model-fabric"


def test_failover_moves_to_next_provider_after_retryable_error():
    pool = FailoverPool()
    calls = []

    class RateLimited(Exception):
        reason = "rate_limited"
        retry_after = 60

    def operation(candidate, provider):
        calls.append((candidate, provider))
        if provider == "provider-a":
            raise RateLimited("quota reached")
        return "ok"

    result = pool.run(
        (("model-a", "provider-a"), ("model-b", "provider-b")),
        operation,
    )
    assert result.value == "ok"
    assert [attempt.candidate for attempt in result.attempts] == ["model-a", "model-b"]
    assert calls == [("model-a", "provider-a"), ("model-b", "provider-b")]
