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


def test_failover_survives_an_exception_that_raises_on_attribute_lookup():
    """A Python 3.8 HTTPError raises ``KeyError('file')`` for unknown names.

    The pool reads ``reason`` and ``retry_after`` off whatever the provider
    raised, so a hostile attribute delegate used to abort the failover decision
    instead of moving to the next provider.
    """
    pool = FailoverPool()
    seen = []

    class Hostile(Exception):
        retry_after = 30.0

        def __getattr__(self, name):
            raise KeyError("file")

    def operation(candidate, provider):
        seen.append((candidate, provider))
        if provider == "provider-a":
            raise Hostile("HTTP Error 429: Too Many Requests")
        return "ok"

    result = pool.run(
        (("model-a", "provider-a"), ("model-b", "provider-b")),
        operation,
    )

    assert result.value == "ok"
    assert seen == [("model-a", "provider-a"), ("model-b", "provider-b")]
    # The declared retry_after is still honoured for the failed provider.
    assert pool.state("provider-a").cooling_down is True
