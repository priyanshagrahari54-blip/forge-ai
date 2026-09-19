from forge.agents.fleet import DEFAULT_AGENT_FLEET
from forge.agents.routing import model_request_for_agent, route_snapshot, route_spec
from forge.models.capabilities import ALL_CAPABILITIES


def test_every_fleet_slot_has_deterministic_routing_contract():
    """Every slot routes by canonical capability, deterministically.

    ``route_spec`` deliberately emits *only* capabilities from the canonical
    Model Fabric vocabulary: "frontend" or "planner" are agent labels, not
    model capabilities, and inventing them would make routing fail closed
    against every real model (no provider advertises them). The slot's own
    domain/specialty therefore travel as request metadata and appear in
    ``route_snapshot``, which is what this contract asserts.
    """
    assert len(DEFAULT_AGENT_FLEET) >= 1000
    for slot in DEFAULT_AGENT_FLEET:
        spec = route_spec(slot)
        assert spec.capabilities
        assert set(spec.capabilities) <= set(ALL_CAPABILITIES), slot.name
        # Deterministic: the same slot always produces the same spec.
        assert route_spec(slot) == spec
        snapshot = route_snapshot(slot)
        assert snapshot["domain"] == slot.domain
        assert snapshot["specialty"] == slot.specialty
        request = model_request_for_agent(slot)
        assert request.caller == "mediated-agent:" + slot.name
        assert tuple(request.required_capabilities) == spec.capabilities
        assert request.metadata["agent_name"] == slot.name
        assert request.metadata["agent_domain"] == slot.domain
        assert request.metadata["agent_specialty"] == slot.specialty


def test_routing_never_claims_a_model_was_selected():
    slot = next(s for s in DEFAULT_AGENT_FLEET if s.name == "security-security-auditor")
    snapshot = route_snapshot(slot)
    assert snapshot["execution"] == "delegated-to-model-fabric"
    assert "selected_model" not in snapshot


def test_specialist_complexity_is_stable():
    planner = next(s for s in DEFAULT_AGENT_FLEET if s.name == "backend-planner")
    builder = next(s for s in DEFAULT_AGENT_FLEET if s.name == "backend-builder")
    assert route_spec(planner).complexity > route_spec(builder).complexity
