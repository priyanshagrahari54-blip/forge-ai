import pytest

from forge.models.policy import DEFAULT_FALLBACK_ORDER, RoutingPolicy


def test_default_policy_is_free_and_local_first():
    policy = RoutingPolicy()
    assert policy.prefer_free is True
    assert policy.prefer_local is True
    assert policy.allow_remote is True
    assert policy.allow_paid is True


def test_policy_validation():
    RoutingPolicy(min_reliability=0.5, max_cost_per_token=1e-6, max_latency_ms=10).validate()
    with pytest.raises(ValueError):
        RoutingPolicy(min_reliability=2.0).validate()
    with pytest.raises(ValueError):
        RoutingPolicy(max_cost_per_token=-1).validate()
    with pytest.raises(ValueError):
        RoutingPolicy(fallback_order=("bogus",)).validate()


def test_policy_roundtrip():
    policy = RoutingPolicy(prefer_free=False, allow_paid=False, min_reliability=0.3)
    restored = RoutingPolicy.from_dict(policy.to_dict())
    assert restored.prefer_free is False
    assert restored.allow_paid is False
    assert restored.min_reliability == 0.3
    assert restored.fallback_order == DEFAULT_FALLBACK_ORDER
