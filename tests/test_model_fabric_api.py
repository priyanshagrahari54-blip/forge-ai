import json

from forge.models.config import FabricConfig
from forge.models.fabric import ModelFabric
from forge.models.policy import RoutingPolicy
from forge.models.provider import ProviderRegistry
from forge.models.registry import Model, ModelRegistry
from forge.models.request import ModelRequest


class DiscoverableProvider:
    name = "discover"

    def list_models(self):
        return ["llava:7b", "codellama:7b"]

    def generate(self, prompt, *, context="", task=""):
        return __import__("forge.models.provider", fromlist=["ModelResult"]).ModelResult("ok", self.name)


def test_default_model_preference():
    fabric = ModelFabric.from_defaults(FabricConfig.from_dict({"default_model": "ollama/llama3.2"}))
    # With no other models, routing still resolves the default (candidates
    # contain it). A fake preferred name that is not registered is ignored.
    decision = fabric.route(ModelRequest(prompt="x", capability="coding"))
    assert decision.chosen


def test_request_alias_and_select():
    fabric = ModelFabric.from_defaults()
    response = fabric.request(ModelRequest(prompt="hi", capability="coding"))
    assert hasattr(response, "success")
    selected = fabric.select(capability="coding")
    assert selected is not None
    assert selected.name.startswith("ollama/")


def test_models_for_capability_and_available():
    fabric = ModelFabric.from_defaults()
    coders = fabric.models_for_capability("coding")
    assert any("ollama/" in model.name for model in coders)
    assert fabric.available_models()
    assert fabric.models_for_capability("vision") == []


def test_discover_models_registers_new_models():
    fabric = ModelFabric.from_defaults()
    fabric.providers.register("discover", DiscoverableProvider())
    result = fabric.discover_models()
    assert "discover" in result
    assert "discover/llava:7b" in [model.name for model in fabric.models()]
    assert "discover/codellama:7b" in [model.name for model in fabric.models()]
    # llava is a known vision family, so discovery derives vision capability.
    assert fabric.registry.get("discover/llava:7b").supports_vision is True
    assert fabric.registry.get("discover/codellama:7b").supports_vision is False


def test_discover_models_handles_failure():
    class Broken:
        name = "broken"

        def list_models(self):
            raise RuntimeError("nope")

        def generate(self, prompt, *, context="", task=""):
            raise RuntimeError("nope")

    fabric = ModelFabric.from_defaults()
    fabric.providers.register("broken", Broken())
    result = fabric.discover_models()
    assert "error" in result["broken"]


def test_provider_health_aggregates():
    fabric = ModelFabric.from_defaults()
    health = fabric.provider_health()
    assert "local" in health and "ollama" in health
    assert health["ollama"]["models"] == 1


def test_stream_falls_back_to_generate_for_non_streaming_providers():
    from forge.models.provider import ModelResult

    class Plain:
        name = "plain"

        def generate(self, prompt, *, context="", task=""):
            return ModelResult("full text", self.name)

    fabric = ModelFabric(
        registry=ModelRegistry([Model(name="plain/model", provider="plain", capabilities=("coding",))]),
        providers=ProviderRegistry({"plain": Plain()}),
    )
    chunks = list(fabric.stream(ModelRequest(prompt="hi", capability="coding")))
    assert chunks == ["full text"]


def test_stream_uses_provider_stream_when_available():
    from forge.models.provider import ModelResult

    class Streaming:
        name = "streaming"

        def generate(self, prompt, *, context="", task=""):
            return ModelResult("", self.name)

        def stream(self, prompt, *, context="", task=""):
            yield "hel"
            yield "lo"

    fabric = ModelFabric(
        registry=ModelRegistry([Model(name="s/model", provider="streaming", capabilities=("coding",))]),
        providers=ProviderRegistry({"streaming": Streaming()}),
    )
    chunks = list(fabric.stream(ModelRequest(prompt="hi", capability="coding")))
    assert "".join(chunks) == "hello"


def test_record_result_alias():
    fabric = ModelFabric.from_defaults()
    fabric.record_result("ollama/llama3.2", True, capability="coding", latency_ms=5.0)
    assert fabric.router.history[-1]["success"] is True


def test_config_fields_roundtrip(tmp_path):
    data = {
        "default_model": "ollama/x",
        "default_policy": "local",
        "preferred_provider": "ollama",
        "local_only": True,
        "free_only": True,
        "max_retries": 5,
        "timeout_seconds": 30,
    }
    path = tmp_path / "models.json"
    path.write_text(json.dumps(data))
    config = FabricConfig.load(path)
    assert config.default_model == "ollama/x"
    assert config.default_policy == "local"
    assert config.preferred_provider == "ollama"
    assert config.local_only is True
    assert config.free_only is True
    assert config.max_retries == 5
    assert config.timeout_seconds == 30


def test_local_only_and_free_only_config_constrain_policy():
    config = FabricConfig.from_dict({"local_only": True, "free_only": True})
    fabric = ModelFabric.from_defaults(config)
    assert fabric.policy.allow_remote is False
    assert fabric.policy.allow_paid is False
    assert fabric.policy.prefer_local is True


def test_privacy_policy_rejects_remote_model():
    # A remote-only model must NOT be selected under a privacy (local-only,
    # non-best-effort) policy; the fabric fails rather than breaching posture.
    remote = Model(name="remote/model", provider="remote", capabilities=("coding",),
                   local=False, free=False)
    fabric = ModelFabric(
        registry=ModelRegistry([remote]),
        providers=ProviderRegistry({}),
        policy=RoutingPolicy.preset("privacy"),
    )
    decision = fabric.route(ModelRequest(prompt="x", capability="coding"))
    assert decision.model is None
    # The fallback ladder never relaxed the remote/paid posture.
    assert "remote" not in decision.fallback_reason
    assert "paid" not in decision.fallback_reason
    response = fabric.request(ModelRequest(prompt="x", capability="coding"))
    assert response.success is False


def test_preferred_provider_is_honored():
    a = Model(name="a/model", provider="provider-a", capabilities=("coding",), reliability=1.0)
    b = Model(name="b/model", provider="provider-b", capabilities=("coding",), reliability=0.6)
    fabric = ModelFabric(
        registry=ModelRegistry([a, b]),
        providers=ProviderRegistry({}),
        config=FabricConfig.from_dict({"preferred_provider": "provider-b"}),
    )
    # Even though provider-a is more reliable, the explicit provider preference
    # wins (b passes capability and is a non-fallback model).
    decision = fabric.route(ModelRequest(prompt="x", capability="coding"))
    assert decision.model.name == "b/model"


def test_default_model_wins_over_preferred_provider():
    a = Model(name="a/model", provider="provider-a", capabilities=("coding",))
    b = Model(name="b/model", provider="provider-b", capabilities=("coding",))
    fabric = ModelFabric(
        registry=ModelRegistry([a, b]),
        providers=ProviderRegistry({}),
        config=FabricConfig.from_dict({"default_model": "a/model", "preferred_provider": "provider-b"}),
    )
    decision = fabric.route(ModelRequest(prompt="x", capability="coding"))
    assert decision.model.name == "a/model"


def test_model_contract_derived_properties():
    model = Model(name="m", provider="p", capabilities=("coding", "reasoning", "vision", "tool_use"))
    assert model.supports_code is True
    assert model.supports_reasoning is True
    assert model.supports_vision is True
    assert model.supports_tools is True
    assert model.supports_structured_output is False
    assert model.supports_streaming is False
    assert model.supports_browser is False
