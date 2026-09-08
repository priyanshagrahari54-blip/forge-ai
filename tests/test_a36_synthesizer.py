"""Text-to-speech layer (A36): deterministic, labeled simulation audio."""
from __future__ import annotations

import pytest

from forge.voice.codec import decode_text
from forge.voice.synthesizer import (SimulatedSpeechSynthesizer, SynthesisError,
                                     UnconfiguredSpeechSynthesizer)


def test_simulated_synthesis_is_deterministic_wav():
    synth = SimulatedSpeechSynthesizer()
    assert synth.available() and synth.simulation
    first = synth.synthesize("run the tests")
    second = synth.synthesize("run the tests")
    assert first.data == second.data
    assert first.wav()[:4] == b"RIFF"
    assert first.duration_ms > 0
    # The simulated utterance round-trips through the simulated recognizer.
    assert decode_text(first.samples()) == "run the tests"


def test_simulated_rejects_empty_and_oversized_text():
    synth = SimulatedSpeechSynthesizer()
    with pytest.raises(SynthesisError) as info:
        synth.synthesize("")
    assert info.value.kind == "invalid_text"
    with pytest.raises(SynthesisError):
        synth.synthesize("x" * 201)


def test_unconfigured_fails_with_not_configured():
    synth = UnconfiguredSpeechSynthesizer()
    assert not synth.available()
    with pytest.raises(SynthesisError) as info:
        synth.synthesize("anything")
    assert info.value.kind == "not_configured"
