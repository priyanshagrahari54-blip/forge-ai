from forge.agents.fleet import DEFAULT_AGENT_FLEET
from forge.agents.routing import model_request_for_agent, route_snapshot, route_spec


def test_every_fleet_slot_has_deterministic_routing_contract():
    assert len(DEFAULT_AGENT_FLEET) >= 1000
    for slot in DEFAULT_AGENT_FLEET:
        spec = route_spec(slot)
        assert spec.capabilities
        assert slot.domain in spec.capabilities or slot.specialty in spec.capabilities
        request = model_request_for_agent(slot)
        assert request.caller == "mediated-agent:" + slot.name
        assert tuple(request.required_capabilities) == spec.capabilities
        assert request.metadata["agent_name"] == slot.name


def test_routing_never_claims_a_model_was_selected():
    slot = next(s for s in DEFAULT_AGENT_FLEET if s.name == "security-security-auditor")
    snapshot = route_snapshot(slot)
    assert snapshot["execution"] == "delegated-to-model-fabric"
    assert "selected_model" not in snapshot


def test_specialist_complexity_is_stable():
    planner = next(s for s in DEFAULT_AGENT_FLEET if s.name == "backend-planner")
    builder = next(s for s in DEFAULT_AGENT_FLEET if s.name == "backend-builder")
    assert route_spec(planner).complexity > route_spec(builder).complexity
