from forge.capabilities import CapabilityCandidate, CapabilityRegistry

def test_registry_only_selects_verified_secure_candidates():
    registry = CapabilityRegistry([
        CapabilityCandidate("browser-computer-use","candidate-a","https://example.invalid/a","adapter",security_status="unverified",verification_status="verified",quality_score=.99),
        CapabilityCandidate("browser-computer-use","candidate-b","https://example.invalid/b","adapter",security_status="reviewed",verification_status="verified",quality_score=.80),
    ])
    assert registry.choose("browser-computer-use").name == "candidate-b"

def test_missing_capabilities_are_explicit():
    assert CapabilityRegistry().missing(["browser-computer-use"]) == ["browser-computer-use"]
