from forge.capabilities import CapabilityBroker, CapabilityCandidate, CapabilityRegistry


def test_broker_resolves_only_verified_capabilities():
    registry = CapabilityRegistry([
        CapabilityCandidate(
            capability="coding",
            name="verified-adapter",
            source="https://example.invalid/adapter",
            interface="adapter",
            security_status="reviewed",
            verification_status="verified",
            quality_score=0.9,
        )
    ])
    result = CapabilityBroker(registry).resolve("coding")
    assert result.available is True
    assert result.candidate is not None


def test_broker_reports_capability_gap_instead_of_faking_availability():
    result = CapabilityBroker(CapabilityRegistry()).resolve("game-engine")
    assert result.available is False
    assert result.candidate is None
