"""Streaming reliability tests (A31 hardening).

Streaming must carry the same guarantees as generate(): routing, capability
and availability checks, failover, health/reliability/latency feedback,
telemetry, error reporting, and deterministic behavior. These tests pin each
of those guarantees.
"""
import pytest

from forge.models.errors import ModelUnavailableError
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry
from forge.models.request import ModelRequest


def _fabric(*pairs):
    return ModelFabric(
        registry=ModelRegistry([
            Model(name=name, provider=provider_name, capabilities=("coding",))
            for name, provider_name in pairs
        ]),
        providers=ProviderRegistry({name: provider for name, provider in _PROVIDERS.items()}),
    )


class ChunkProvider:
    name = "chunk"

    def generate(self, prompt, *, context="", task=""):
        return ModelResult("hello", self.name)

    def stream(self, prompt, *, context="", task=""):
        yield "hel"
        yield "lo"


class FailingStream:
    name = "failing"

    def generate(self, prompt, *, context="", task=""):
        return ModelResult("", self.name)

    def stream(self, prompt, *, context="", task=""):
        yield "partial-bad"
        raise RuntimeError("connection timed out")


class AlwaysFails:
    name = "always-fails"

    def generate(self, prompt, *, context="", task=""):
        raise RuntimeError("provider down")

    def stream(self, prompt, *, context="", task=""):
        raise RuntimeError("provider down")
        yield  # pragma: no cover


class PlainGenerate:
    name = "plain"

    def generate(self, prompt, *, context="", task=""):
        return ModelResult("single-chunk", self.name)


_PROVIDERS = {
    "chunk": ChunkProvider(),
    "failing": FailingStream(),
    "always-fails": AlwaysFails(),
    "plain": PlainGenerate(),
}


# -- successful streaming ---------------------------------------------------

def test_stream_success_yields_chunks_and_records_telemetry():
    fabric = _fabric(("chunk/m", "chunk"))
    chunks = list(fabric.stream(ModelRequest(prompt="hi", capability="coding")))
    assert chunks == ["hel", "lo"]
    assert fabric.telemetry.count("response") == 1
    assert fabric.telemetry.count("route") == 1
    model = fabric.registry.get("chunk/m")
    assert model.health.total_successes == 1
    assert model.health.consecutive_failures == 0
    assert model.latency_ms > 0
    assert fabric.router.history[-1]["success"] is True


# -- provider without streaming ---------------------------------------------

def test_stream_falls_back_to_single_chunk_for_non_streaming_provider():
    fabric = _fabric(("plain/m", "plain"))
    chunks = list(fabric.stream(ModelRequest(prompt="hi", capability="coding")))
    assert chunks == ["single-chunk"]
    assert fabric.telemetry.count("response") == 1
    assert fabric.registry.get("plain/m").health.total_successes == 1


# -- streaming failure → failover -------------------------------------------

def test_stream_fails_over_without_duplicate_or_partial_output():
    fabric = ModelFabric(
        registry=ModelRegistry([
            # "afailing/m" sorts first, so it is the chosen primary and the
            # healthy "chunk/m" is the failover candidate.
            Model(name="afailing/m", provider="failing", capabilities=("coding",)),
            Model(name="chunk/m", provider="chunk", capabilities=("coding",)),
        ]),
        providers=ProviderRegistry({"failing": FailingStream(), "chunk": ChunkProvider()}),
    )
    chunks = list(fabric.stream(ModelRequest(prompt="hi", capability="coding")))
    # The failing provider's partial chunk must never leak into the output.
    assert chunks == ["hel", "lo"]
    history = fabric.router.history
    assert any(event["model"] == "afailing/m" and not event["success"] for event in history)
    assert any(event["model"] == "chunk/m" and event["success"] for event in history)
    assert fabric.telemetry.count("response") == 1


def test_stream_all_providers_fail_raises():
    fabric = _fabric(("always-fails/m", "always-fails"))
    with pytest.raises(ModelUnavailableError):
        list(fabric.stream(ModelRequest(prompt="hi", capability="coding")))
    assert fabric.telemetry.count("error") == 1
    assert fabric.registry.get("always-fails/m").health.total_failures == 1
    assert fabric.router.history[-1]["success"] is False


def test_stream_routing_failure_raises():
    fabric = ModelFabric(
        registry=ModelRegistry([Model(name="vision-only/m", provider="chunk", capabilities=("vision",))]),
        providers=ProviderRegistry({"chunk": ChunkProvider()}),
    )
    with pytest.raises(ModelUnavailableError):
        list(fabric.stream(ModelRequest(prompt="hi", capability="coding")))
    assert fabric.telemetry.count("error") == 1


# -- timeout / error propagation --------------------------------------------

def test_stream_propagates_provider_error_into_fallback():
    fabric = ModelFabric(
        registry=ModelRegistry([
            Model(name="always-fails/m", provider="always-fails", capabilities=("coding",)),
            Model(name="plain/m", provider="plain", capabilities=("coding",)),
        ]),
        providers=ProviderRegistry({"always-fails": AlwaysFails(), "plain": PlainGenerate()}),
    )
    chunks = list(fabric.stream(ModelRequest(prompt="hi", capability="coding")))
    assert chunks == ["single-chunk"]
    feedback = [event for event in fabric.router.history if event["model"] == "always-fails/m"]
    assert feedback and feedback[-1]["success"] is False
    assert feedback[-1]["error"] == "provider down"
