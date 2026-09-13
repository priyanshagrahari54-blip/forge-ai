"""A81 HYBRID router tests: the full deterministic decision matrix."""
from __future__ import annotations

from forge.client.estimator import KIND_ENGINEERING, KIND_INSPECT, estimate
from forge.client.router import OUTCOME_LOCAL, OUTCOME_REFUSED, OUTCOME_SERVER
from forge.client.resources import ResourceSnapshot
from forge.client.router import ServerStatus, decide
from forge.client.config import LocalPolicy

GOOD_RESOURCES = ResourceSnapshot(cpus=2, total_ram_mb=4096,
                                  free_ram_mb=3000, source="test")
LOW_RESOURCES = ResourceSnapshot(cpus=1, total_ram_mb=2048,
                                 free_ram_mb=100, source="test")
SERVER_UP = ServerStatus(reachable=True, model_ready=True, workers=4)
SERVER_DOWN = ServerStatus(reachable=False, model_ready=False, workers=0)

LIGHT = "show repository status"
HEAVY = "implement a CSV export feature"
BIG_LIGHT = "show " + "detail " * 500  # > default 2000 chars


# -- estimator -------------------------------------------------------------------

def test_estimator_is_deterministic():
    assert estimate(LIGHT) == estimate(LIGHT)
    assert estimate(HEAVY) == estimate(HEAVY)


def test_estimator_kinds():
    assert estimate(LIGHT).kind == KIND_INSPECT
    assert not estimate(LIGHT).needs_model
    assert estimate(HEAVY).kind == KIND_ENGINEERING
    assert estimate(HEAVY).needs_model
    # Ambiguous tasks fail towards the server.
    assert estimate("something vague").kind == KIND_ENGINEERING
    # Engineering signals win over inspection signals.
    assert estimate("check and fix the login bug").kind == KIND_ENGINEERING


# -- mode: server ------------------------------------------------------------------

def test_mode_server_routes_to_server():
    decision = decide("server", LIGHT, LocalPolicy(), SERVER_DOWN, None)
    assert decision.execution == "SERVER"
    assert decision.reason == "mode=server"


def test_mode_server_refused_by_policy():
    policy = LocalPolicy(allow_server=False)
    decision = decide("server", LIGHT, policy, SERVER_UP, GOOD_RESOURCES)
    assert decision.execution == OUTCOME_REFUSED
    assert "allow_server=false" in decision.reason


# -- mode: local --------------------------------------------------------------------

def test_mode_local_light_task_executes_locally():
    decision = decide("local", LIGHT, LocalPolicy(), SERVER_UP,
                      GOOD_RESOURCES)
    assert decision.execution == "LOCAL"
    assert "light local task" in decision.reason


def test_mode_local_refuses_model_task_even_when_server_available():
    """Explicit LOCAL with failing gates is an honest REFUSED — the
    user picked LOCAL; the router never silently re-routes."""
    decision = decide("local", HEAVY, LocalPolicy(), SERVER_UP,
                      GOOD_RESOURCES)
    assert decision.execution == OUTCOME_REFUSED
    assert "task requires a model" in decision.reason
    assert "switch to SERVER or HYBRID" in decision.reason


def test_mode_local_refuses_oversized_task():
    decision = decide("local", BIG_LIGHT, LocalPolicy(), SERVER_UP,
                      GOOD_RESOURCES)
    assert decision.execution == OUTCOME_REFUSED
    assert "task too large" in decision.reason


# -- mode: hybrid (the automatic selector) -----------------------------------------------

def test_hybrid_light_task_when_server_up():
    decision = decide("hybrid", LIGHT, LocalPolicy(), SERVER_UP,
                      GOOD_RESOURCES)
    assert decision.execution == "LOCAL"
    assert "light local task" in decision.reason


def test_hybrid_engineering_task_goes_to_server():
    decision = decide("hybrid", HEAVY, LocalPolicy(), SERVER_UP,
                      GOOD_RESOURCES)
    assert decision.execution == "SERVER"
    assert "task requires a model" in decision.reason


def test_hybrid_oversized_light_task_goes_to_server():
    decision = decide("hybrid", BIG_LIGHT, LocalPolicy(), SERVER_UP,
                      GOOD_RESOURCES)
    assert decision.execution == "SERVER"
    assert "task too large" in decision.reason


def test_hybrid_low_resources_goes_to_server():
    decision = decide("hybrid", LIGHT, LocalPolicy(), SERVER_UP,
                      LOW_RESOURCES)
    assert decision.execution == "SERVER"
    assert "insufficient local resources" in decision.reason


def test_hybrid_server_down_light_task_runs_locally():
    decision = decide("hybrid", LIGHT, LocalPolicy(), SERVER_DOWN,
                      GOOD_RESOURCES)
    assert decision.execution == "LOCAL"
    assert "server unreachable" in decision.reason


def test_hybrid_server_down_heavy_task_fails_honestly():
    decision = decide("hybrid", HEAVY, LocalPolicy(), SERVER_DOWN,
                      GOOD_RESOURCES)
    assert decision.execution == "SERVER"
    assert "will fail until the server returns" in decision.reason


def test_hybrid_policy_disallow_local_never_runs_local():
    policy = LocalPolicy(allow_local=False)
    decision = decide("hybrid", LIGHT, policy, SERVER_UP, GOOD_RESOURCES)
    assert decision.execution == "SERVER"
    assert "allow_local=false" in decision.reason
    with_server_down = decide("hybrid", LIGHT, policy, SERVER_DOWN,
                              GOOD_RESOURCES)
    # The decision targets the server but is honestly marked as one
    # that will fail until the server returns (the client raises
    # SERVER_UNREACHABLE instead of submitting).
    assert with_server_down.execution == "SERVER"
    assert "will fail until the server returns" in with_server_down.reason


def test_hybrid_policy_disallow_server_refuses_when_local_gates_fail():
    policy = LocalPolicy(allow_server=False)
    decision = decide("hybrid", HEAVY, policy, SERVER_UP, GOOD_RESOURCES)
    assert decision.execution == OUTCOME_REFUSED
    assert "allow_server=false" in decision.reason
    # ... but a light task still runs locally.
    light = decide("hybrid", LIGHT, policy, SERVER_UP, GOOD_RESOURCES)
    assert light.execution == OUTCOME_LOCAL


def test_no_resource_snapshot_fails_closed():
    decision = decide("hybrid", LIGHT, LocalPolicy(), SERVER_UP, None)
    assert decision.execution == "SERVER"
    assert "no resource snapshot" in decision.reason


def test_decision_is_deterministic_for_identical_inputs():
    first = decide("hybrid", HEAVY, LocalPolicy(), SERVER_UP,
                   GOOD_RESOURCES)
    second = decide("hybrid", HEAVY, LocalPolicy(), SERVER_UP,
                    GOOD_RESOURCES)
    assert first == second


def test_refused_is_never_labelled_as_execution():
    """The three outcomes are disjoint and every refusal says why."""
    policy = LocalPolicy(allow_server=False)
    for requirement in (HEAVY, BIG_LIGHT):
        decision = decide("server", requirement, policy, SERVER_UP,
                          GOOD_RESOURCES)
        assert decision.execution == OUTCOME_REFUSED
        assert decision.reason.startswith("refused:")
        assert not decision.is_local and not decision.execution == \
            OUTCOME_SERVER
