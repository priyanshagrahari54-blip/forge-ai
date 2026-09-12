"""A81 — the AI Engine requests inference through the runtime interface.

``forge.models.runtime_bridge`` sits on the *engine* side of the boundary: the
fabric keeps its own routing/policy/telemetry, and the runtime stays
independent.  These tests prove the adapter is faithful — including that a
runtime failure is propagated instead of being turned into output.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a81 import (  # noqa: E402
    ExplodingBackend, ModelServingBackend, ScriptedBackend, make_runtime,
)

from forge.models import ModelFabric, ModelRequest  # noqa: E402
from forge.models.runtime_bridge import RuntimeProvider, attach_runtime  # noqa: E402
from forge.runtime.model_runtime import RuntimeRequest  # noqa: E402


def write_model(root: Path, name: str = "tiny.gguf") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 5, 2) + b"\0" * 32)
    return path


def native_runtime(tmp_path, name: str = "tiny.gguf"):
    """A runtime whose only backend is the native one over ``tmp_path``."""
    from forge.runtime.model_runtime import (ModelRuntime, NativeBackend,
                                             RuntimeConfig)

    models = tmp_path / "models"
    write_model(models, name)
    backend = NativeBackend(model_dirs=(str(models),))
    config = RuntimeConfig(default_backend="native", backends=("native",),
                           timeout_seconds=5.0, max_timeout_seconds=10.0)
    return ModelRuntime(config, backends=[backend])


# -- provider adapter --------------------------------------------------------


def test_provider_forwards_the_fabric_call_into_the_runtime():
    backend = ScriptedBackend(name="serving", response="runtime answered")
    runtime = make_runtime(backend, default_backend="serving",
                           timeout_seconds=5.0)
    provider = RuntimeProvider(runtime, backend="serving", model="m1")

    result = provider.generate("write code", context="repo", task="export",
                               instructions="no deps", max_output_tokens=32,
                               temperature=0.1)
    assert result.text == "runtime answered"
    assert result.model == "m1"

    sent = backend.generate_calls[0]
    assert isinstance(sent, RuntimeRequest)
    assert sent.prompt == "write code"
    assert sent.context == "repo"
    assert sent.task == "export"
    assert sent.instructions == "no deps"
    assert sent.max_output_tokens == 32
    assert sent.temperature == 0.1
    assert sent.backend == "serving"


def test_provider_raises_on_a_runtime_failure_like_every_other_provider():
    runtime = make_runtime(ExplodingBackend(name="boom"),
                           default_backend="boom")
    provider = RuntimeProvider(runtime)
    with pytest.raises(RuntimeError) as exc:
        provider.generate("write code")
    assert "could not run inference" in str(exc.value)
    assert ExplodingBackend.SECRET not in str(exc.value)


def test_provider_refuses_a_backend_that_cannot_infer(tmp_path):
    runtime = native_runtime(tmp_path)
    provider = RuntimeProvider(runtime, backend="native")
    with pytest.raises(RuntimeError) as exc:
        provider.generate("write code")
    assert "inference adapter" in str(exc.value).lower()


def test_provider_streams_through_the_runtime():
    backend = ScriptedBackend(name="serving", chunks=["a", "b", "c"])
    runtime = make_runtime(backend, default_backend="serving",
                           timeout_seconds=5.0)
    provider = RuntimeProvider(runtime, backend="serving")
    assert list(provider.stream("prompt")) == ["a", "b", "c"]


def test_provider_stream_failure_is_reported():
    runtime = make_runtime(ExplodingBackend(name="boom"),
                           default_backend="boom", timeout_seconds=5.0)
    provider = RuntimeProvider(runtime, backend="boom")
    with pytest.raises(RuntimeError) as exc:
        list(provider.stream("prompt"))
    assert "streaming failed" in str(exc.value).lower()


def test_provider_health_and_discovery_views():
    serving = ModelServingBackend(name="serving", models=["alpha", "beta"])
    runtime = make_runtime(serving, default_backend="serving")
    provider = RuntimeProvider(runtime, backend="serving")

    assert sorted(provider.list_models()) == ["alpha", "beta"]
    health = provider.health()
    assert health["available"] is True
    assert health["ready_backends"] == ["serving"]


def test_provider_requires_a_runtime():
    with pytest.raises(ValueError):
        RuntimeProvider(None)


# -- fabric integration ------------------------------------------------------


def test_attach_runtime_registers_a_provider_and_models(tmp_path):
    runtime = native_runtime(tmp_path)
    fabric = ModelFabric.from_defaults()
    before = sorted(model.name for model in fabric.models())

    registered = attach_runtime(fabric, runtime, backend="native",
                                name="runtime")

    assert registered == ["runtime/tiny.gguf"]
    assert fabric.providers.has("runtime") is True
    after = sorted(model.name for model in fabric.models())
    # Additive only: nothing the fabric already had was removed or changed.
    assert set(before).issubset(set(after))
    model = fabric.registry.get("runtime/tiny.gguf")
    assert model.provider == "runtime"
    assert model.local is True
    assert model.metadata["runtime_model_id"] == "native:tiny.gguf"
    assert model.metadata["runtime_format"] == "gguf"
    assert model.metadata["size_bytes"] == 56


def test_attach_runtime_is_idempotent_for_known_models(tmp_path):
    runtime = native_runtime(tmp_path)
    fabric = ModelFabric.from_defaults()
    attach_runtime(fabric, runtime, name="runtime")
    # A second fabric keeps its own registry; the runtime state is shared.
    other = ModelFabric.from_defaults()
    assert attach_runtime(other, runtime, name="runtime") == [
        "runtime/tiny.gguf"]


def test_attach_runtime_without_model_registration(tmp_path):
    runtime = native_runtime(tmp_path)
    fabric = ModelFabric.from_defaults()
    assert attach_runtime(fabric, runtime, register_models=False) == []
    assert fabric.providers.has("runtime") is True


def test_attach_runtime_validates_its_arguments():
    fabric = ModelFabric.from_defaults()
    with pytest.raises(ValueError):
        attach_runtime(None, make_runtime(ScriptedBackend(name="a")))
    with pytest.raises(ValueError):
        attach_runtime(fabric, None)


def test_fabric_can_route_a_request_to_a_runtime_backed_model():
    """End to end: fabric routing -> runtime interface -> backend."""
    backend = ScriptedBackend(name="serving", response="routed via runtime")
    serving = ModelServingBackend(name="catalog", models=["alpha"],
                                  response="routed via runtime")
    runtime = make_runtime(backend, serving, default_backend="serving",
                           timeout_seconds=5.0)

    fabric = ModelFabric.from_defaults()
    attach_runtime(fabric, runtime, backend="catalog", name="runtime",
                   capabilities=("coding",))

    response = fabric.generate(ModelRequest(prompt="implement export",
                                            capability="coding"))
    assert response.success is True
    assert response.provider == "runtime"
    assert response.model == "runtime/alpha"
    assert response.text == "routed via runtime"
    assert len(serving.generate_calls) == 1


def test_fabric_reports_failure_when_the_runtime_cannot_infer():
    from forge.models import Model

    runtime = make_runtime(ExplodingBackend(name="boom"),
                           default_backend="boom")
    fabric = ModelFabric.from_defaults()
    fabric.register_provider("runtime", RuntimeProvider(runtime,
                                                        backend="boom"))
    fabric.register_model(Model(name="runtime/broken", provider="runtime",
                                capabilities=("coding",),
                                context_window=4096))

    response = fabric.generate(ModelRequest(prompt="implement export",
                                            capability="coding"))
    # The fabric may fail over to another model after the runtime-backed one
    # fails; either way the engine went through the runtime, and the backend's
    # secret never reaches the response.
    assert [entry["backend"] for entry in runtime.history()] == ["boom"]
    assert ExplodingBackend.SECRET not in json.dumps(response.to_dict(),
                                                     default=str)


def test_default_fabric_is_unchanged_by_the_bridge():
    """Enabling the runtime is opt-in: nothing is wired up by default."""
    fabric = ModelFabric.from_defaults()
    assert fabric.providers.has("runtime") is False
    assert all(not model.name.startswith("runtime/")
               for model in fabric.models())

    # Importing the bridge does not register anything either.
    import forge.models  # noqa: F401

    fresh = ModelFabric.from_defaults()
    assert fresh.providers.has("runtime") is False
    response = fresh.generate(ModelRequest(prompt="reply ok",
                                           capability="coding"))
    assert response.provider in ("local", "ollama", "openai")
