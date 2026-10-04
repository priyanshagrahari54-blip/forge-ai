from forge.core.requirement_intelligence import ContractStatus, analyze

def test_contract_has_quality_and_acceptance_gates():
    contract = analyze("Build a professional video editing application")
    assert "creative-media" in contract.capabilities
    assert contract.quality_requirements
    assert contract.acceptance_criteria
    assert "Do not report completion without evidence." in contract.failure_conditions

def test_quality_without_target_is_visible_not_hidden():
    contract = analyze("Build an AAA game")
    assert contract.status == ContractStatus.NEEDS_CLARIFICATION.value
    assert contract.ambiguities

def test_simple_requirement_can_be_executable():
    contract = analyze("Add a login endpoint")
    assert contract.status == ContractStatus.READY.value
    assert contract.executable
