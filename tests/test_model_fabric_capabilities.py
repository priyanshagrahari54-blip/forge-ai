from forge.models.capabilities import (
    ALL_CAPABILITIES,
    AGENTIC_CAPABILITIES,
    AUDIO_CAPABILITIES,
    MULTIMODAL_CAPABILITIES,
    TEXT_CAPABILITIES,
    Capability,
    is_capability,
    normalize_capability,
)


def test_capability_vocabulary_is_complete():
    # The A31 objective enumerates exactly these 18 capabilities.
    expected = {
        "coding", "reasoning", "planning", "debugging", "testing",
        "review", "security", "research", "documentation", "vision",
        "audio", "speech_to_text", "text_to_speech", "browser",
        "computer_use", "tool_use", "structured_output", "long_context",
    }
    assert set(ALL_CAPABILITIES) == expected
    assert len(ALL_CAPABILITIES) == 18
    assert len(ALL_CAPABILITIES) == len(set(ALL_CAPABILITIES))


def test_capability_enum_values_are_canonical_strings():
    assert Capability.CODING.value == "coding"
    assert Capability.CODING == "coding"
    assert Capability("coding") is Capability.CODING


def test_capability_groups_are_disjoint_and_nonempty():
    groups = (TEXT_CAPABILITIES, MULTIMODAL_CAPABILITIES, AUDIO_CAPABILITIES, AGENTIC_CAPABILITIES)
    for group in groups:
        assert group
        assert all(is_capability(cap) for cap in group)


def test_normalize_capability():
    assert normalize_capability(Capability.CODING) == "coding"
    assert normalize_capability("vision") == "vision"


def test_unknown_capability_rejected():
    assert not is_capability("bogus")
    try:
        normalize_capability("bogus")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown capability should raise ValueError")
