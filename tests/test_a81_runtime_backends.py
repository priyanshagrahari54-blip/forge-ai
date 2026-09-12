"""A81 — the pluggable backends: native, Ollama, llama.cpp, custom Forge.

Every artifact in these tests is a synthetic header, not a model: the runtime
reports what the bytes actually say and nothing more.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a81 import StubClient, make_runtime  # noqa: E402

from forge.runtime.model_runtime import (  # noqa: E402
    BackendKind, BackendUnavailableError, ExternalClientBackend,
    ForgeInferenceBackend, LlamaCppBackend, ModelNotFoundError, ModelRuntime,
    NativeBackend,
    OllamaBackend, RuntimeConfig, RuntimeModel, RuntimeRequest, RuntimeState,
    RuntimeCancelledError, create_backend, describe_artifact, describe_gguf,
    describe_safetensors,
)


def write_gguf(path: Path, version: int = 3, tensors: int = 12,
               entries: int = 5) -> Path:
    path.write_bytes(b"GGUF" + struct.pack("<IQQ", version, tensors, entries)
                     + b"\0" * 32)
    return path


def write_safetensors(path: Path, tensors: int = 4,
                      metadata: dict | None = None) -> Path:
    header: dict = dict(metadata or {})
    payload = {"__metadata__": header}
    for index in range(tensors):
        payload["weight.%d" % index] = {"dtype": "F32", "shape": [8],
                                        "data_offsets": [0, 32]}
    body = json.dumps(payload).encode("utf-8")
    path.write_bytes(struct.pack("<Q", len(body)) + body)
    return path


# -- native discovery and metadata -------------------------------------------


def test_native_discovery_reads_real_headers(tmp_path):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    write_gguf(model_dir / "tiny-q4.gguf", version=3, tensors=42, entries=17)
    write_safetensors(model_dir / "flat.safetensors", tensors=4,
                      metadata={"format": "pt", "architecture": "llama"})

    backend = NativeBackend(model_dirs=(str(model_dir),))
    models = {model.name: model for model in backend.list_models()}

    gguf = models["tiny-q4.gguf"]
    assert gguf.model_id == "native:tiny-q4.gguf"
    assert gguf.backend == "native"
    assert gguf.format == "gguf"
    assert gguf.metadata["gguf_version"] == 3
    assert gguf.metadata["tensor_count"] == 42
    assert gguf.metadata["metadata_entries"] == 17
    assert gguf.metadata["header_ok"] is True
    assert gguf.size_bytes == (model_dir / "tiny-q4.gguf").stat().st_size
    # Never fabricated: the runtime does not know the context window here.
    assert gguf.context_window == 0
    assert gguf.parameters == ""

    flat = models["flat.safetensors"]
    assert flat.format == "safetensors"
    assert flat.metadata["tensor_count"] == 4
    assert flat.metadata["declared_architecture"] == "llama"


def test_header_parsers_report_honestly_for_garbage(tmp_path):
    broken = tmp_path / "broken.gguf"
    broken.write_bytes(b"not a gguf file at all, just some bytes")
    assert describe_gguf(str(broken))["header_ok"] is False
    assert describe_artifact(str(broken))["header_ok"] is False

    truncated = tmp_path / "trunc.safetensors"
    truncated.write_bytes(b"\xff" * 4)
    assert describe_safetensors(str(truncated))["header_ok"] is False

    huge = tmp_path / "huge.safetensors"
    huge.write_bytes(struct.pack("<Q", 10 ** 12) + b"{}")
    assert describe_safetensors(str(huge))["header_ok"] is False

    missing = tmp_path / "absent.gguf"
    assert describe_gguf(str(missing))["header_ok"] is False


def test_unsupported_formats_are_not_discovered(tmp_path):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    write_gguf(model_dir / "ok.gguf")
    (model_dir / "notes.txt").write_text("hello", encoding="utf-8")
    (model_dir / "weights.bin").write_bytes(b"\x80\x04")
    (model_dir / "model.onnx").write_bytes(b"\x08" * 16)

    models = NativeBackend(model_dirs=(str(model_dir),)).list_models()
    assert sorted(model.name for model in models) == ["model.onnx", "ok.gguf"]


def test_native_generation_refuses_to_fabricate_output(tmp_path):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    write_gguf(model_dir / "tiny.gguf")

    runtime = make_runtime(NativeBackend(model_dirs=(str(model_dir),)),
                           default_backend="native")
    runtime.discover("native")
    response = runtime.generate(RuntimeRequest(prompt="write code",
                                               model="native:tiny.gguf"))

    assert response.success is False
    assert response.error_kind == "unavailable"
    assert "inference adapter" in response.error.lower()
    assert response.text == ""


def test_native_backend_delegates_to_an_explicit_adapter(tmp_path):
    from forge.runtime.model_runtime import (InferenceAdapter, RuntimeResponse)

    class _Adapter(InferenceAdapter):
        name = "test-adapter"

        def available(self):
            return (True, "adapter present")

        def generate(self, request, token=None):
            return RuntimeResponse(text="adapter produced this",
                                   model=request.model, output_tokens=3)

        def stream(self, request, token=None):
            for chunk in ("ad", "apter"):
                yield chunk

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    write_gguf(model_dir / "tiny.gguf")
    backend = NativeBackend(model_dirs=(str(model_dir),), adapter=_Adapter())
    runtime = make_runtime(backend, default_backend="native",
                           timeout_seconds=5.0)

    assert runtime.health()[0].status == RuntimeState.READY.value
    response = runtime.generate(RuntimeRequest(prompt="x",
                                               model="native:tiny.gguf"))
    assert response.success is True
    assert response.text == "adapter produced this"

    stream = runtime.stream(RuntimeRequest(prompt="x"))
    assert "".join(chunk.text for chunk in stream) == "adapter"


def test_native_load_and_unload_lifecycle(tmp_path):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    write_gguf(model_dir / "tiny.gguf")
    backend = NativeBackend(model_dirs=(str(model_dir),))
    runtime = make_runtime(backend, default_backend="native")
    runtime.discover("native")

    model = runtime.load("native:tiny.gguf")
    assert model.loaded is True
    assert runtime.models(loaded_only=True)[0].model_id == "native:tiny.gguf"
    assert runtime.health()[0].models_loaded == 1
    assert backend.resources()["loaded_models"] == 1
    assert backend.resources()["resident_bytes"] == model.size_bytes

    assert runtime.unload("native:tiny.gguf") is True
    assert runtime.models(loaded_only=True) == []
    assert runtime.unload("native:tiny.gguf") is False
    with pytest.raises(ModelNotFoundError):
        runtime.get_model("native:absent.gguf")


def test_native_load_rejects_an_outside_path(tmp_path):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    outside = tmp_path / "outside.gguf"
    write_gguf(outside)

    backend = NativeBackend(model_dirs=(str(model_dir),))
    runtime = make_runtime(backend, default_backend="native")
    # Register the model by hand with a path outside the allowlist: the load
    # must refuse it even though the runtime "knows" the model.
    runtime._models["native:outside.gguf"] = RuntimeModel(
        model_id="native:outside.gguf", name="outside.gguf",
        backend="native", path=str(outside))
    with pytest.raises(BackendUnavailableError) as exc:
        runtime.load("native:outside.gguf")
    assert "outside" in str(exc.value).lower()


def test_discovery_reports_ambiguous_names_and_refresh_semantics(tmp_path):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    write_gguf(model_dir / "tiny.gguf")
    backend = NativeBackend(model_dirs=(str(model_dir),))
    runtime = make_runtime(backend, default_backend="native")

    first = runtime.discover("native")
    assert first["native"]["refreshed"] is True
    assert first["native"]["discovered"] == ["native:tiny.gguf"]

    cached = runtime.discover("native", refresh=False)
    assert cached["native"]["refreshed"] is False
    assert cached["native"]["discovered"] == ["native:tiny.gguf"]

    # Resolving by bare name works when it is unambiguous.
    assert runtime.resolve_model("tiny.gguf").model_id == "native:tiny.gguf"
    with pytest.raises(ModelNotFoundError):
        runtime.resolve_model("absent.gguf")


# -- Ollama backend ----------------------------------------------------------


def test_ollama_endpoint_is_normalized_once():
    for url in ("http://127.0.0.1:11434", "http://127.0.0.1:11434/",
                "http://127.0.0.1:11434/api/generate",
                "http://127.0.0.1:11434/api/tags"):
        assert OllamaBackend(base_url=url).base_url == "http://127.0.0.1:11434"
    with pytest.raises(ValueError):
        OllamaBackend(base_url="")


def test_ollama_request_body_carries_every_part_of_the_request():
    backend = OllamaBackend(allow_network=True, default_model="llama3.2")
    request = RuntimeRequest(prompt="do it", task="add export",
                             context="repo context",
                             instructions="no new deps",
                             max_output_tokens=64, temperature=0.2,
                             seed=7, stop=("END",), model="qwen2.5:7b")
    body = backend._body(request, stream=False)

    assert body["model"] == "qwen2.5:7b"
    assert body["stream"] is False
    assert body["system"] == "add export"
    assert "do it" in body["prompt"]
    assert "repo context" in body["prompt"]
    assert "no new deps" in body["prompt"]
    assert body["options"] == {"num_predict": 64, "temperature": 0.2,
                               "seed": 7, "stop": ["END"]}


def test_ollama_model_selection_strips_the_backend_prefix():
    backend = OllamaBackend(allow_network=True, default_model="llama3.2")
    assert backend._model_name(RuntimeRequest(model="ollama:qwen2.5")) == \
        "qwen2.5"
    assert backend._model_name(RuntimeRequest(model="qwen2.5")) == "qwen2.5"
    assert backend._model_name(RuntimeRequest()) == "llama3.2"


def test_ollama_generate_requires_a_model_name():
    backend = OllamaBackend(allow_network=True)
    with pytest.raises(BackendUnavailableError):
        backend.generate(RuntimeRequest(prompt="x"))


def test_ollama_discovery_parses_the_tags_payload(monkeypatch):
    payload = {"models": [
        {"name": "llama3.2", "size": 2000,
         "details": {"family": "llama", "parameter_size": "3B",
                     "quantization_level": "Q4_K_M", "context_window": 8192}},
        {"name": "llava:7b", "size": 4000, "details": {"family": "llava"}},
        {"size": 1},
    ]}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _Response())

    models = OllamaBackend(allow_network=True).list_models()
    assert [model.name for model in models] == ["llama3.2", "llava:7b"]
    first = models[0]
    assert first.model_id == "ollama:llama3.2"
    assert first.size_bytes == 2000
    assert first.quantization == "Q4_K_M"
    assert first.parameters == "3B"
    assert first.metadata["family"] == "llama"
    assert first.metadata["context_window"] == 8192


def test_ollama_health_reports_unreachable_endpoints(monkeypatch):
    import urllib.request

    def _boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    health = OllamaBackend(allow_network=True).health(probe=True)
    assert health.status == RuntimeState.UNAVAILABLE.value
    assert "connection refused" in health.error
    assert health.latency_ms >= 0.0


def test_ollama_stream_parses_ndjson_and_honours_cancellation(monkeypatch):
    from forge.runtime.model_runtime import CancellationToken

    lines = [
        json.dumps({"response": "Hel", "done": False}),
        json.dumps({"response": "lo", "done": False}),
        b"",
        b"not json",
        json.dumps({"response": "!", "done": True, "eval_count": 9}),
        json.dumps({"response": "IGNORED", "done": False}),
    ]

    class _Response:
        def __init__(self):
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __iter__(self):
            for line in lines:
                yield line if isinstance(line, bytes) else line.encode("utf-8")

        def close(self):
            self.closed = True

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _Response())
    backend = OllamaBackend(allow_network=True, default_model="llama3.2")

    token = CancellationToken()
    chunks = list(backend.stream(RuntimeRequest(prompt="x"), token))
    # Blank and unparsable lines are skipped; everything after done is not.
    assert [chunk.text for chunk in chunks] == ["Hel", "lo", "!"]
    assert chunks[-1].output_tokens == 9

    # A request cancelled before it starts never reaches the endpoint.
    cancelled = CancellationToken()
    cancelled.cancel()
    with pytest.raises(RuntimeCancelledError):
        list(backend.stream(RuntimeRequest(prompt="x"), cancelled))


def test_ollama_generate_parses_the_completion(monkeypatch):
    payload = {"response": "generated text", "done": True,
               "done_reason": "stop", "prompt_eval_count": 5,
               "eval_count": 2, "total_duration": 1234}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _Response())
    result = OllamaBackend(allow_network=True,
                           default_model="llama3.2").generate(
        RuntimeRequest(prompt="x"))
    assert result.success is True
    assert result.text == "generated text"
    assert result.input_tokens == 5
    assert result.output_tokens == 2
    assert result.finish_reason == "stop"
    assert result.metadata["total_duration_ns"] == 1234


def test_ollama_unparsable_payloads_fail_honestly(monkeypatch):
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"<html>not json</html>"

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _Response())
    backend = OllamaBackend(allow_network=True, default_model="llama3.2")
    with pytest.raises(BackendUnavailableError):
        backend.list_models()
    with pytest.raises(BackendUnavailableError):
        backend.generate(RuntimeRequest(prompt="x"))


# -- llama.cpp / custom Forge backends ---------------------------------------


def test_client_backends_adapt_an_explicit_client():
    client = StubClient(response="client answer", models=["stub-model"])
    for backend, expected_name, expected_kind in (
            (LlamaCppBackend(client=client), "llama_cpp",
             BackendKind.LLAMA_CPP.value),
            (ForgeInferenceBackend(client=client), "forge",
             BackendKind.FORGE.value),
            (ExternalClientBackend(name="custom", client=client), "custom",
             BackendKind.CUSTOM.value)):
        assert backend.name == expected_name
        assert backend.kind == expected_kind
        assert backend.available()[0] is True
        assert [model.name for model in backend.list_models()] == ["stub-model"]

        result = backend.generate(RuntimeRequest(prompt="x",
                                                            model="m"))
        assert result.text == "client answer"
        assert [chunk for chunk in backend.stream(RuntimeRequest(
            prompt="x"))] == ["a", "b", "c"]


def test_client_backend_reports_a_client_that_cannot_generate():
    class _Partial:
        def available(self):
            return (True, "no generate here")

    backend = ExternalClientBackend(name="partial", client=_Partial())
    with pytest.raises(BackendUnavailableError):
        backend.generate(RuntimeRequest(prompt="x"))
    with pytest.raises(BackendUnavailableError):
        list(backend.stream(RuntimeRequest(prompt="x")))


def test_client_backend_rejects_a_client_returning_the_wrong_type():
    from forge.runtime.model_runtime import BackendProtocolError

    class _WrongType:
        def generate(self, request, token=None):
            return "just a string"

    backend = ExternalClientBackend(name="wrong", client=_WrongType())
    with pytest.raises(BackendProtocolError):
        backend.generate(RuntimeRequest(prompt="x"))


def test_client_backends_register_and_serve_through_the_runtime():
    client = StubClient(response="served by llama.cpp stub",
                        models=["stub-7b"])
    backend = create_backend("llama_cpp", client=client)
    runtime = make_runtime(backend, default_backend="llama_cpp",
                           timeout_seconds=5.0)
    runtime.discover("llama_cpp")

    assert [model.model_id for model in runtime.models()] == [
        "llama_cpp:stub-7b"]
    response = runtime.generate(RuntimeRequest(
        prompt="x", model="llama_cpp:stub-7b"))
    assert response.success is True
    assert response.text == "served by llama.cpp stub"
    assert client.generated == 1


# -- configuration -----------------------------------------------------------


def test_runtime_config_defaults_are_offline_and_native():
    config = RuntimeConfig.from_dict({}, env={})
    assert config.default_backend == "native"
    assert config.backends == ("native",)
    assert config.allow_network is False
    assert config.model_dirs == ()
    assert config.timeout_seconds == 120.0
    assert config.max_timeout_seconds == 1800.0
    assert config.to_dict()["allow_network"] is False


def test_runtime_config_reads_environment_variables():
    env = {
        "FORGE_RUNTIME_BACKEND": "ollama",
        "FORGE_RUNTIME_BACKENDS": "native,ollama",
        "FORGE_RUNTIME_ALLOW_NETWORK": "true",
        "FORGE_RUNTIME_OLLAMA_URL": "http://10.0.0.5:11434",
        "FORGE_RUNTIME_OLLAMA_MODEL": "qwen2.5",
        "FORGE_RUNTIME_TIMEOUT": "45",
    }
    config = RuntimeConfig.from_dict({}, env=env)
    assert config.default_backend == "ollama"
    assert config.backends == ("native", "ollama")
    assert config.allow_network is True
    assert config.ollama_url == "http://10.0.0.5:11434"
    assert config.ollama_model == "qwen2.5"
    assert config.timeout_seconds == 45.0


def test_naming_a_default_backend_also_enables_it():
    # Asking for the Ollama backend by name is an explicit selection, so it
    # must be constructed rather than failing as an unregistered default.
    config = RuntimeConfig.from_dict(
        {}, env={"FORGE_RUNTIME_BACKEND": "ollama"})
    assert config.default_backend == "ollama"
    assert config.backends == ("native", "ollama")
    assert config.allow_network is False

    runtime = ModelRuntime.from_defaults(config, load_config=False)
    assert runtime.has_backend("ollama") is True
    assert runtime.select_backend().name == "ollama"


def test_runtime_config_rejects_unknown_backends_and_bad_bounds():
    with pytest.raises(ValueError):
        RuntimeConfig(backends=("native", "evil")).validate()
    with pytest.raises(ValueError):
        RuntimeConfig(timeout_seconds=0).validate()
    with pytest.raises(ValueError):
        RuntimeConfig(timeout_seconds=10, max_timeout_seconds=1).validate()
    with pytest.raises(ValueError):
        RuntimeConfig(max_timeout_seconds=10 ** 9).validate()
    with pytest.raises(ValueError):
        RuntimeConfig(default_backend="").validate()
    with pytest.raises(ValueError):
        RuntimeConfig(backends=()).validate()


def test_runtime_config_loads_a_json_file(tmp_path):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({
        "default_backend": "native",
        "backends": ["native"],
        "model_dirs": [str(tmp_path)],
        "timeout_seconds": 30,
    }), encoding="utf-8")

    config = RuntimeConfig.load(str(path), env={})
    assert config.model_dirs == (str(tmp_path),)
    assert config.timeout_seconds == 30.0


def test_runtime_config_load_without_a_file_uses_defaults(tmp_path,
                                                         monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = RuntimeConfig.load(env={})
    assert config.default_backend == "native"
    assert config.allow_network is False
