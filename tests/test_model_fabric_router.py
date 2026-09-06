from forge.models.policy import RoutingPolicy
from forge.models.registry import Model, ModelRegistry
from forge.models.request import ModelRequest
from forge.models.router import FabricRouter


def make_router(models, policy=None):
    return FabricRouter(ModelRegistry(models), policy=policy or RoutingPolicy())


def test_capability_aware_routing():
    coder = Model(name="coder", provider="p", capabilities=("coding", "debugging"))
    seer = Model(name="seer", provider="p", capabilities=("vision",))
    router = make_router([coder, seer])
    assert router.route(ModelRequest(prompt="fix a bug", capability="coding")).model.name == "coder"
    assert router.route(ModelRequest(prompt="describe", capability="vision")).model.name == "seer"
    # required capabilities must ALL be satisfied
    both = router.route(ModelRequest(prompt="x", capability="coding", required_capabilities=("coding", "vision")))
    assert both.model is None
    assert "coding" in both.error and "vision" in both.error


def test_context_aware_routing():
    small = Model(name="small", provider="p", capabilities=("coding",), context_window=4096)
    large = Model(name="large", provider="p", capabilities=("coding",), context_window=32768)
    router = make_router([small, large])
    chosen = router.route(ModelRequest(prompt="x", capability="coding", min_context_window=8000))
    assert chosen.model.name == "large"


def test_complexity_aware_routing():
    small = Model(name="small-ctx", provider="p", capabilities=("coding",), context_window=4096)
    large = Model(name="large-ctx", provider="p", capabilities=("coding",), context_window=16384)
    router = make_router([small, large])
    complex_choice = router.route(ModelRequest(prompt="x", capability="coding", complexity=8.0))
    assert complex_choice.model.name == "large-ctx"


def test_cost_free_local_policy():
    free_local = Model(name="free-local", provider="p", capabilities=("coding",), free=True, local=True)
    paid_remote = Model(name="paid-remote", provider="p", capabilities=("coding",),
                        free=False, local=False, cost_per_token=1e-6, reliability=1.0)

    free_first = make_router([free_local, paid_remote], RoutingPolicy(prefer_free=True, prefer_local=True))
    assert free_first.route(ModelRequest(prompt="x", capability="coding")).model.name == "free-local"

    no_paid = make_router([free_local, paid_remote], RoutingPolicy(allow_paid=False))
    assert no_paid.route(ModelRequest(prompt="x", capability="coding")).model.name == "free-local"

    no_remote = make_router([free_local, paid_remote], RoutingPolicy(allow_remote=False))
    assert no_remote.route(ModelRequest(prompt="x", capability="coding")).model.name == "free-local"

    # With preferences disabled, the more reliable paid model can win.
    neutral = make_router([free_local, paid_remote], RoutingPolicy(prefer_free=False, prefer_local=False))
    free_local.reliability = 0.6
    assert neutral.route(ModelRequest(prompt="x", capability="coding")).model.name == "paid-remote"


def test_deterministic_fallback_relaxes_policy():
    slow = Model(name="slow", provider="p", capabilities=("coding",), latency_ms=500.0)
    router = make_router([slow], RoutingPolicy(max_latency_ms=10.0))
    decision = router.route(ModelRequest(prompt="x", capability="coding"))
    assert decision.model.name == "slow"
    assert decision.fallback is True
    assert "latency" in decision.fallback_reason


def test_unsupported_capability_never_falls_back():
    coder = Model(name="coder", provider="p", capabilities=("coding",))
    router = make_router([coder])
    decision = router.route(ModelRequest(prompt="x", capability="vision"))
    assert decision.model is None
    assert decision.fallback is False
    assert "vision" in decision.error


def test_health_tracking_and_last_resort_fallback():
    model = Model(name="flaky", provider="p", capabilities=("coding",))
    router = make_router([model])
    # Drive the model unhealthy through feedback.
    for _ in range(model.health.unhealthy_after):
        router.record("flaky", False, latency_ms=10.0, capability="coding", error="down")
    assert model.health.status == "unhealthy"
    # Strict routing excludes unhealthy models; the health fallback step uses
    # it as a last resort.
    decision = router.route(ModelRequest(prompt="x", capability="coding"))
    assert decision.model.name == "flaky"
    assert decision.fallback is True
    assert "health" in decision.fallback_reason


def test_feedback_updates_reliability_latency_and_history():
    model = Model(name="m", provider="p", capabilities=("coding",))
    router = make_router([model])
    router.record("m", True, latency_ms=200.0, capability="coding", task_complexity=2.0)
    router.record("m", False, latency_ms=400.0, capability="coding")
    assert model.health.total_successes == 1
    assert model.health.total_failures == 1
    assert model.latency_ms == 300.0  # EMA of 200 then 400
    assert model.reliability < 1.0
    assert router.history[0]["success"] is True
    assert router.history[1]["failure"] is True
    assert "latency" in router.history[0]
    assert router.history[0]["task_complexity"] == 2.0


def test_select_compatibility_arguments():
    small = Model(name="small", provider="p", capabilities=("coding",), context_window=4096)
    large = Model(name="large", provider="p", capabilities=("coding",), context_window=16384)
    router = make_router([small, large])
    assert router.select("coding", min_context_size=8000).name == "large"


def test_regular_models_beat_fallback_models():
    regular = Model(name="real", provider="p", capabilities=("coding",))
    fallback = Model(name="noop", provider="p", capabilities=("coding",), fallback=True)
    router = make_router([fallback, regular])
    decision = router.route(ModelRequest(prompt="x", capability="coding"))
    assert decision.model.name == "real"
    # The no-op still appears at the end of the candidate chain for failover.
    assert decision.candidates[-1] == "noop"
