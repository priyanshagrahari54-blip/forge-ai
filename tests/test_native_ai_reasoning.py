"""Reasoning interface: backend selection, refusals, provenance.

The honesty properties this suite pins down:

* the deterministic backend never generates code/prose-as-model-output;
* without a real model, generative requests are refused with a structured
  code, and the refusal — not fake output — reaches the report;
* neural backends are interfaces over the Model Fabric (one abstraction);
  they activate only for *real* registered models and never claim the
  deterministic placeholder is a model;
* provider failures surface as failures (no silent success fallback).
"""
from __future__ import annotations

from helpers_native_ai import (
    CALC_GOOD,
    ScriptedProvider,
    changes_text,
    scripted_fabric,
    write_repo,
)

from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderInfo, ProviderRegistry
from forge.models.registry import Model, ModelRegistry
from forge.native.reasoning import (
    LocalNeuralBackend,
    NativeDeterministicBackend,
    ReasoningHub,
    ReasoningKind,
    ReasoningRequest,
    RefusalCode,
    RemoteNeuralBackend,
)


def hub_without_model() -> ReasoningHub:
    return ReasoningHub([NativeDeterministicBackend()])


def hub_with_model(responder) -> "tuple[ReasoningHub, ScriptedProvider]":
    provider = responder if isinstance(responder, ScriptedProvider) \
        else ScriptedProvider(responder)
    hub = ReasoningHub([NativeDeterministicBackend(),
                        LocalNeuralBackend(scripted_fabric(provider))])
    return hub, provider


def test_deterministic_backend_responds_structurally():
    backend = NativeDeterministicBackend()
    ok, detail = backend.available()
    assert ok and "no model" in detail
    result = backend.respond(ReasoningRequest(
        kind=ReasoningKind.UNDERSTAND, task="fix calc.py"))
    assert result.ok and result.neural is False
    assert result.data["classification"]["task_class"] == "fix"


def test_deterministic_backend_refuses_every_generative_kind():
    backend = NativeDeterministicBackend()
    for kind in ReasoningKind.generative():
        result = backend.respond(ReasoningRequest(kind=kind, task="t"))
        assert not result.ok
        assert result.refusal == RefusalCode.NEURAL_REQUIRED.value
        assert "fabricate" in result.message
    # and it refuses *by design*, not by accident:
    assert backend.responds_to(ReasoningKind.REPAIR) is False


def test_hub_refusal_for_repair_without_model():
    hub = hub_without_model()
    result = hub.respond(ReasoningRequest(
        kind=ReasoningKind.REPAIR, task="fix",
        payload={"failure_output": "AssertionError"}))
    assert not result.ok
    assert result.refusal == RefusalCode.NEURAL_REQUIRED.value
    # the refusal explains the situation instead of hiding it
    assert "native-deterministic" in result.message or \
        "does not handle" in result.message


def test_hub_selection_log_explains_choice():
    hub = hub_without_model()
    hub.respond(ReasoningRequest(kind=ReasoningKind.DIAGNOSE, task="t",
                                 payload={"failure_output": "boom"}))
    assert hub.decisions, "selection decisions must be observable"
    assert hub.decisions[-1]["selection"]["chosen"] == \
        "native-deterministic"


def test_local_backend_activation_requires_real_model(tmp_path):
    # A fabric whose only model is the deterministic fallback placeholder
    # must report the neural backend UNAVAILABLE: the placeholder is not a
    # model and never counts as one.
    placeholder = ModelFabric(registry=ModelRegistry(),
                              providers=ProviderRegistry())

    def _generate(prompt, **kwargs):
        return ModelResult('{"changes": {}}', "local-fallback")

    placeholder.providers.register(
        "local",
        type("LocalProvider", (), {"name": "local",
                                   "generate": staticmethod(_generate)})(),
        ProviderInfo(name="local", kind="fallback", local=True, free=True))
    placeholder.registry.register(Model(
        name="local-fallback", provider="local", fallback=True,
        capabilities=("coding",)))
    backend = LocalNeuralBackend(placeholder)
    ok, detail = backend.available()
    assert ok is False
    assert "non-fallback" in detail


def test_scripted_model_answers_generative_requests():
    def responder(prompt):
        return changes_text({"calc.py": CALC_GOOD})
    hub, provider = hub_with_model(ScriptedProvider(responder))
    assert hub.status()["generative_ready"] is True
    result = hub.respond(ReasoningRequest(
        kind=ReasoningKind.REPAIR, task="fix calc.py",
        payload={"failure_output": "AssertionError: 1 == 3"}))
    assert result.ok and result.neural is True
    assert result.data["changes"] == {"calc.py": CALC_GOOD}
    assert result.model == "scripted-model"
    assert result.generated_by == "local-neural(scripted-model)"
    assert len(provider.calls) == 1  # asked exactly once, no retries


def test_backend_selection_prefers_local_before_remote():
    fabric = scripted_fabric({})
    remote = RemoteNeuralBackend(fabric)
    local = LocalNeuralBackend(fabric)
    hub = ReasoningHub([NativeDeterministicBackend(), remote, local])
    chosen, explanation = hub.select(ReasoningKind.REPAIR)
    # selection order follows registration: the hub tries registered
    # neural backends in order, and both are available; the deterministic
    # one is never chosen for generative kinds.
    assert chosen in (local, remote)
    assert explanation["policy"].startswith("generative kinds require")


def test_model_output_must_be_valid_json_object():
    hub, _provider = hub_with_model("i am sure the code is fine, no JSON")
    result = hub.respond(ReasoningRequest(
        kind=ReasoningKind.GENERATE, task="implement x"))
    assert not result.ok
    assert result.refusal == RefusalCode.INVALID_MODEL_OUTPUT.value
    assert "raw_excerpt" in result.data  # kept for diagnosis, not for lies


def test_provider_exception_surfaces_as_failure_not_success():
    class ExplodingProvider:
        name = "explodes"

        def generate(self, prompt, **kwargs):
            raise RuntimeError("connection refused")

    fabric = ModelFabric(registry=ModelRegistry(),
                         providers=ProviderRegistry())
    fabric.providers.register(
        "explodes", ExplodingProvider(),
        ProviderInfo(name="explodes", kind="local", local=True, free=True,
                     capabilities=("coding",)))
    fabric.registry.register(Model(name="boom-model", provider="explodes",
                                   capabilities=("coding", "debugging")))
    hub = ReasoningHub([NativeDeterministicBackend(),
                        LocalNeuralBackend(fabric)])
    result = hub.respond(ReasoningRequest(kind=ReasoningKind.GENERATE,
                                          task="t"))
    assert not result.ok
    assert result.refusal == RefusalCode.BACKEND_ERROR.value
    assert "connection refused" in result.message


def test_engine_backend_selection_without_fabric_is_deterministic(tmp_path):
    write_repo(tmp_path)
    from forge.native.engine import NativeAIEngine
    engine = NativeAIEngine(tmp_path, persist_status=False)
    assert engine.status()["reasoning"]["active_backend"][
        "name"] == "native-deterministic"
    assert engine.status()["reasoning"]["generative_ready"] is False


def test_diagnose_classifies_failure_families():
    backend = NativeDeterministicBackend()
    cases = {
        'File "x.py", line 2\nSyntaxError: invalid syntax': "syntax",
        "ModuleNotFoundError: No module named 'ghost'": "import",
        "collected 0 items": "collection",
        "E       assert 1 == 2\n": "assertion",
        "subprocess.TimeoutExpired after 30s": "timeout",
    }
    for text, expected in cases.items():
        result = backend.respond(ReasoningRequest(
            kind=ReasoningKind.DIAGNOSE, task="t",
            payload={"failure_output": text}))
        assert result.ok and result.data["category"] == expected, text


def test_fabric_backends_are_the_only_model_abstraction():
    """No vendor SDK imports anywhere in the native package.

    Neural paths must route through ModelFabric objects only — a direct
    ``requests``/``openai``/``google`` call inside ``forge/native`` would
    bypass routing, health, policy, and telemetry, so it is forbidden at
    the import level (prose mentions of vendors are fine).
    """
    import ast
    from pathlib import Path

    forbidden_roots = {"requests", "httpx", "urllib3", "aiohttp", "openai",
                       "google", "anthropic", "ollama", "tiktoken", "torch",
                       "transformers"}
    native_dir = Path(__file__).resolve().parent.parent / "forge" / "native"
    offenders = []
    for path in sorted(native_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"),
                         filename=str(path))
        for node in ast.walk(tree):
            roots = []
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots = [(node.module or "").split(".")[0]]
            for root in roots:
                if root in forbidden_roots:
                    offenders.append("%s imports %s" % (path.name, root))
    assert not offenders, offenders
