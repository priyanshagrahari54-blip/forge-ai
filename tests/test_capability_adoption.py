from forge.capabilities.adoption import AdoptionDecision, AdoptionPolicy, decide
from forge.capabilities.registry import CapabilityCandidate


def candidate(**kwargs):
    base = dict(
        capability="coding", name="upstream-coding", source="https://github.com/example/project",
        interface="adapter", license="MIT", security_status="reviewed",
        verification_status="verified", quality_score=.90, strategy="adapt",
    )
    base.update(kwargs)
    return CapabilityCandidate(**base)


def test_verified_candidate_is_adopted_through_strategy():
    assert decide(candidate()) is AdoptionDecision.ADAPT


def test_unverified_candidate_is_blocked():
    assert decide(candidate(verification_status="unverified")) is AdoptionDecision.BLOCK


def test_missing_license_is_blocked_by_default():
    assert decide(candidate(license="")) is AdoptionDecision.BLOCK


def test_quality_threshold_is_explicit():
    assert decide(candidate(quality_score=.5)) is AdoptionDecision.BLOCK
    assert decide(candidate(quality_score=.5), policy=AdoptionPolicy(min_quality=.4)) is AdoptionDecision.ADAPT
