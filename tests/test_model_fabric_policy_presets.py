import pytest

from forge.models.policy import RoutingPolicy


def test_presets_exist_and_are_distinct():
    for name in ("quality", "balanced", "fast", "free", "local", "privacy"):
        policy = RoutingPolicy.preset(name)
        assert policy is not None
    with pytest.raises(ValueError):
        RoutingPolicy.preset("bogus")


def test_free_preset_blocks_paid():
    policy = RoutingPolicy.preset("free")
    assert policy.allow_paid is False
    assert policy.max_cost_per_token == 0.0


def test_local_and_privacy_presets_block_remote():
    assert RoutingPolicy.preset("local").allow_remote is False
    privacy = RoutingPolicy.preset("privacy")
    assert privacy.allow_remote is False
    assert privacy.allow_paid is False
    assert privacy.use_best_effort is False


def test_privacy_preset_does_not_relax_remote_or_paid():
    # The privacy posture must never silently contact remote/paid providers.
    privacy = RoutingPolicy.preset("privacy")
    ladder_steps = set()
    from forge.models.router import FabricRouter
    for relaxed in FabricRouter._ladder(privacy):
        ladder_steps.update(relaxed)
    assert "remote" not in ladder_steps
    assert "paid" not in ladder_steps


def test_resolve_for_request_is_immutable():
    policy = RoutingPolicy(prefer_free=True, allow_remote=True)
    overridden = policy.resolve_for_request(prefer_free=False)
    assert policy.prefer_free is True
    assert overridden.prefer_free is False
    assert overridden.allow_remote is True
