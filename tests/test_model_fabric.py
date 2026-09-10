import json

import pytest

from forge.models.config import FabricConfig
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry
from forge.models.request import ModelRequest
from forge.models.router import ModelRouter


class ScriptedProvider:
    name = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def generate(self, prompt, *, context="", task=""):
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return ModelResult(self.responses[index], self.name)


class BoomProvider:
    name = "boom"

    def generate(self, prompt, *, context="", task=""):
        raise RuntimeError("boom")


def make_fabric(models, providers, policy=None):
    registry = ModelRegistry(models)
    provider_registry = ProviderRegistry(providers)
    return ModelFabric(registry=registry, providers=provider_registry, policy=policy)


def test_from_defaults_has_ollama_and_local_fallback():
    fabric = ModelFabric.from_defaults()
    names = {model.name for model in fabric.models()}
    assert "local-fallback" in names
    assert "ollama/llama3.2" in names
    fallback = fabric.registry.get("local-fallback")
    assert fallback.fallback is True
    assert fallback.free and fallback.local
    # No network access at construction time.
    assert fabric.providers.has("ollama")


def test_default_routing_prefers_real_model_over_noop():
    fabric = ModelFabric.from_defaults()
    decision = fabric.route(ModelRequest(prompt="hi", capability="coding"))
    assert decision.model.name.startswith("ollama/")
    # The deterministic no-op is still in the failover chain.
    assert "local-fallback" in decision.candidates


def test_default_fabric_unsupported_capability_errors():
    fabric = ModelFabric.from_defaults()
    decision = fabric.route(ModelRequest(prompt="describe", capability="vision"))
    assert decision.model is None
    assert "vision" in decision.error


def test_generate_success_records_telemetry_and_feedback():
    model = Model(name="scripted/model", provider="scripted", capabilities=("coding",))
    provider = ScriptedProvider(['{"ok": true}'])
    fabric = make_fabric([model], {"scripted": provider})
    response = fabric.generate(ModelRequest(prompt="hi", capability="coding"))
    assert response.success is True
    assert response.model == "scripted/model"
    assert response.text == '{"ok": true}'
    assert fabric.telemetry.count("route") == 1
    assert fabric.telemetry.count("response") == 1
    assert fabric.telemetry.count("feedback") == 1
    assert fabric.router.history[0]["success"] is True


def test_generate_fails_over_deterministically():
    boom = Model(name="boom/model", provider="boom", capabilities=("coding",))
    fallback = Model(name="noop", provider="noop", capabilities=("coding",), fallback=True)
    noop_provider = ScriptedProvider(['{"changes": {}}'])

    fabric = make_fabric(
        [boom, fallback],
        {"boom": BoomProvider(), "noop": noop_provider},
    )
    response = fabric.generate(ModelRequest(prompt="hi", capability="coding"))
    # The failing provider is recorded as feedback, then failover succeeds.
    assert response.success is True
    assert response.model == "noop"
    assert fabric.registry.get("boom/model").health.total_failures == 1


def test_generate_all_failures_returns_failure_response():
    boom = Model(name="boom/model", provider="boom", capabilities=("coding",))
    fabric = make_fabric([boom], {"boom": BoomProvider()})
    response = fabric.generate(ModelRequest(prompt="hi", capability="coding"))
    assert response.success is False
    assert "boom" in response.error
    assert fabric.telemetry.count("error") == 1


def test_generate_no_capable_model_returns_failure():
    vision = Model(name="seer", provider="seer", capabilities=("vision",))
    fabric = make_fabric([vision], {"seer": ScriptedProvider(["ok"])})
    response = fabric.generate(ModelRequest(prompt="hi", capability="coding"))
    assert response.success is False
    assert "capabilit" in response.error


def test_record_feedback_and_health_snapshot():
    model = Model(name="m", provider="p", capabilities=("coding",))
    fabric = make_fabric([model], {"p": ScriptedProvider(["ok"])})
    fabric.record_feedback(model="m", capability="coding", success=True, latency_ms=30.0)
    fabric.record_feedback(model="m", capability="coding", success=False, error="down")
    health = fabric.health()["m"]
    assert health["health"] != "healthy"
    assert health["latency_ms"] == 30.0


def test_legacy_router_view():
    fabric = ModelFabric.from_defaults()
    legacy = fabric.legacy_router()
    assert isinstance(legacy, ModelRouter)
    chosen = legacy.select("coding")
    assert chosen is not None
    assert chosen.provider is not None
    assert chosen.provider.name == "ollama"


def test_structured_request_response_roundtrip():
    request = ModelRequest(
        prompt="write a test",
        capability="testing",
        required_capabilities=("testing",),
        min_context_window=4096,
        complexity=2.0,
    )
    payload = request.to_dict()
    assert payload["prompt_chars"] == len("write a test")
    assert payload["capability"] == "testing"
    response = ModelRequest.from_prompt("x", capability="coding")
    assert response.capability == "coding"


def test_snapshot_contains_all_parts():
    fabric = ModelFabric.from_defaults()
    snap = fabric.snapshot()
    assert set(snap) == {"models", "providers", "policy", "capabilities",
                         "telemetry_events", "router_history"}
    assert isinstance(snap["policy"], dict)


def test_telemetry_never_contains_prompt_content():
    secret = "PRIVATE-SECRET-TOKEN-12345"
    model = Model(name="m", provider="p", capabilities=("coding",))
    fabric = make_fabric([model], {"p": ScriptedProvider(["ok"])})
    fabric.generate(ModelRequest(prompt=f"use key {secret}", capability="coding"))
    dumped = json.dumps(fabric.telemetry.events())
    assert secret not in dumped


def test_config_file_load(tmp_path):
    cfg_path = tmp_path / "models.json"
    cfg_path.write_text(json.dumps({
        "ollama_model": "llama3.2:1b",
        "policy": {"allow_paid": False},
    }))
    config = FabricConfig.load(cfg_path)
    assert config.ollama_model == "llama3.2:1b"
    assert config.policy.allow_paid is False


def test_register_model_requires_provider():
    fabric = ModelFabric.from_defaults()
    with pytest.raises(ValueError):
        fabric.register_model(Model(name="x", provider="missing-provider", capabilities=("coding",)))
