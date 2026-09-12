"""A81 — runtime security invariants.

Each test locks one documented rule:

1. no secrets in logs / responses / status
2. no arbitrary executable loading (no import-by-name, no code artifacts)
3. no unrestricted filesystem access (explicit directories only)
4. no silent network access (``allow_network`` defaults to False)
5. explicit provider/backend selection (no implicit failover)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a81 import ExplodingBackend, ScriptedBackend, make_runtime  # noqa: E402

from forge.runtime.model_runtime import (  # noqa: E402
    ALLOWED_MODEL_EXTENSIONS, EXECUTABLE_EXTENSIONS, PICKLE_EXTENSIONS,
    BackendNotFoundError, ModelRuntimeError, NativeBackend, OllamaBackend,
    RuntimeConfig, RuntimeRequest, RuntimeSecurityError, classify_artifact,
    create_backend,
)


# -- 1. no secrets in logs ---------------------------------------------------


def test_request_and_response_views_carry_sizes_not_content():
    request = RuntimeRequest(prompt="P" * 50, context="C" * 30,
                             task="export csv", instructions="no deps")
    payload = request.to_dict()
    assert payload["prompt_chars"] == 50
    assert payload["context_chars"] == 30
    assert "P" * 50 not in json.dumps(payload)
    assert "export csv" not in json.dumps(payload)

    response = make_runtime(ScriptedBackend(name="a",
                                            response="R" * 20)).generate(
        RuntimeRequest(prompt="Q"))
    assert "R" * 20 not in json.dumps(response.to_dict(), default=str)


def test_backend_error_messages_are_redacted_everywhere():
    runtime = make_runtime(ExplodingBackend(name="boom"))
    response = runtime.generate(RuntimeRequest(prompt="x", backend="boom"))

    assert ExplodingBackend.SECRET not in response.error
    assert ExplodingBackend.SECRET not in json.dumps(response.to_dict(),
                                                     default=str)
    assert ExplodingBackend.SECRET not in json.dumps(runtime.status(),
                                                     default=str)
    assert ExplodingBackend.SECRET not in json.dumps(runtime.history(10),
                                                     default=str)


def test_streaming_errors_are_wrapped_and_redacted():
    runtime = make_runtime(ExplodingBackend(name="boom"),
                           default_backend="boom", timeout_seconds=5.0)
    stream = runtime.stream(RuntimeRequest(prompt="x"))
    with pytest.raises(ModelRuntimeError) as exc:
        list(stream)
    assert "SUPERSECRETVALUE123" not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)
    assert isinstance(exc.value.__cause__, RuntimeError)


def test_ollama_url_with_embedded_credentials_is_refused():
    with pytest.raises(RuntimeSecurityError):
        OllamaBackend(base_url="http://user:pass@127.0.0.1:11434")
    with pytest.raises(ValueError):
        RuntimeConfig(ollama_url="http://u:p@host").validate()


def test_non_http_ollama_schemes_are_refused():
    for url in ("file:///etc/passwd", "ftp://host", "gopher://host",
                "http+unix:///var/run/x"):
        with pytest.raises(RuntimeSecurityError):
            OllamaBackend(base_url=url)


# -- 2. no arbitrary executable loading --------------------------------------


def test_backends_cannot_be_imported_from_a_string():
    for name in ("os", "forge.tools.terminal", "my.module.Backend",
                 "__import__", "eval", "", "  "):
        with pytest.raises(BackendNotFoundError):
            create_backend(name)


def test_only_the_documented_allowlist_is_builtin():
    from forge.runtime.model_runtime import BUILTIN_BACKENDS

    assert BUILTIN_BACKENDS == ("native", "ollama", "llama_cpp", "forge")


def test_executable_and_pickle_artifacts_are_refused():
    for name in ("model.exe", "model.dll", "model.so", "model.py",
                 "model.pyc", "install.sh", "run.bat", "lib.dylib"):
        extension, refusal = classify_artifact(name)
        assert refusal, name
        assert "executable" in refusal or "library" in refusal

    for name in ("weights.bin", "model.pt", "model.pth", "ckpt.ckpt",
                 "state.pkl", "state.pickle"):
        extension, refusal = classify_artifact(name)
        assert refusal, name
        assert "pickle" in refusal

    for name in ("model.gguf", "model.safetensors", "model.onnx"):
        extension, refusal = classify_artifact(name)
        assert refusal == ""
        assert extension in ALLOWED_MODEL_EXTENSIONS


def test_allowlists_are_disjoint_and_pickle_formats_are_banned():
    assert not set(ALLOWED_MODEL_EXTENSIONS) & set(EXECUTABLE_EXTENSIONS)
    assert not set(ALLOWED_MODEL_EXTENSIONS) & set(PICKLE_EXTENSIONS)
    assert ".bin" in PICKLE_EXTENSIONS
    assert ".bin" in EXECUTABLE_EXTENSIONS


def test_native_backend_refuses_to_load_a_pickle_checkpoint(tmp_path):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    pickle_path = model_dir / "weights.bin"
    pickle_path.write_bytes(b"\x80\x04\x95fake pickle")

    backend = NativeBackend(model_dirs=(str(model_dir),))
    with pytest.raises(RuntimeSecurityError) as exc:
        backend.assert_loadable(str(pickle_path))
    assert "pickle" in str(exc.value)


def test_native_backend_refuses_executables_even_inside_an_allowed_dir(
        tmp_path):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    script = model_dir / "loader.py"
    script.write_text("print('executed')", encoding="utf-8")

    backend = NativeBackend(model_dirs=(str(model_dir),))
    with pytest.raises(RuntimeSecurityError):
        backend.assert_loadable(str(script))
    # And it is never discovered as a model either.
    assert backend.list_models() == []


def test_optional_engines_are_never_imported_by_the_runtime():
    for module in ("llama_cpp", "llama_cpp_python", "torch", "transformers",
                   "onnxruntime", "ctransformers"):
        assert module not in sys.modules, module

    llama = create_backend("llama_cpp")
    forge = create_backend("forge")
    assert llama.available()[0] is False
    assert forge.available()[0] is False
    assert "explicitly provided client" in llama.available()[1]

    for module in ("llama_cpp", "llama_cpp_python", "torch", "transformers",
                   "onnxruntime", "ctransformers"):
        assert module not in sys.modules, module


def test_client_backends_refuse_inference_without_a_client():
    llama = create_backend("llama_cpp")
    with pytest.raises(ModelRuntimeError):
        llama.generate(RuntimeRequest(prompt="x"))
    with pytest.raises(ModelRuntimeError):
        list(llama.stream(RuntimeRequest(prompt="x")))


def test_runtime_module_does_not_import_the_ai_engine():
    """The runtime is independent of the AI Engine (one-way dependency)."""
    import ast

    module = (Path(__file__).resolve().parent.parent / "forge" / "runtime"
              / "model_runtime.py")
    source = module.read_text(encoding="utf-8")
    tree = ast.parse(source)
    # Only the module body counts: guarded, function-local optional imports
    # (PyYAML for config files, ctypes for Windows memory) are not part of
    # the mandatory dependency surface.
    imported = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {"forge.core", "forge.agents", "forge.control",
                 "forge.models", "forge.api", "forge.desktop_app"}
    assert not (imported & forbidden), sorted(imported & forbidden)
    # Stdlib only: no third-party requirement is introduced.
    third_party = {name for name in imported
                   if name not in ("json", "logging", "os", "platform",
                                   "queue", "re", "struct", "threading",
                                   "time", "urllib", "collections",
                                   "dataclasses", "enum", "typing", "uuid",
                                   "ctypes", "__future__")}
    assert not third_party, sorted(third_party)


# -- 3. no unrestricted filesystem access ------------------------------------


def test_native_discovery_only_sees_configured_directories(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    (allowed / "ok.gguf").write_bytes(b"GGUF" + b"\0" * 60)
    (outside / "hidden.gguf").write_bytes(b"GGUF" + b"\0" * 60)

    backend = NativeBackend(model_dirs=(str(allowed),))
    names = [model.name for model in backend.list_models()]
    assert names == ["ok.gguf"]


def test_native_backend_refuses_paths_outside_its_directories(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "ok.gguf").write_bytes(b"GGUF" + b"\0" * 60)
    secret = tmp_path / "secret.gguf"
    secret.write_bytes(b"GGUF" + b"\0" * 60)

    backend = NativeBackend(model_dirs=(str(allowed),))
    with pytest.raises(RuntimeSecurityError):
        backend.assert_inside(str(secret))
    with pytest.raises(RuntimeSecurityError):
        backend.assert_inside("/etc/passwd")
    with pytest.raises(RuntimeSecurityError):
        backend.assert_inside(str(allowed / ".." / "secret.gguf"))


def test_native_backend_without_directories_touches_nothing():
    backend = NativeBackend(model_dirs=())
    assert backend.list_models() == []
    with pytest.raises(RuntimeSecurityError) as exc:
        backend.assert_inside("anything.gguf")
    assert "No model directories" in str(exc.value)


def test_discovery_does_not_follow_symlinks_out_of_the_allowlist(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    (outside / "linked.gguf").write_bytes(b"GGUF" + b"\0" * 60)
    try:
        os.symlink(str(outside), str(allowed / "escape"))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this platform")

    backend = NativeBackend(model_dirs=(str(allowed),))
    assert backend.list_models() == []


def test_discovery_depth_and_count_are_bounded(tmp_path):
    root = tmp_path / "models"
    deep = root
    for _ in range(8):
        deep = deep / "level"
    deep.mkdir(parents=True)
    (deep / "too-deep.gguf").write_bytes(b"GGUF" + b"\0" * 60)
    (root / "shallow.gguf").write_bytes(b"GGUF" + b"\0" * 60)

    backend = NativeBackend(model_dirs=(str(root),), max_depth=2)
    assert [model.name for model in backend.list_models()] == ["shallow.gguf"]

    # The file bound applies too: discovery stops after max_files artifacts.
    (root / "second.gguf").write_bytes(b"GGUF" + b"\0" * 60)
    (root / "third.gguf").write_bytes(b"GGUF" + b"\0" * 60)
    bounded = NativeBackend(model_dirs=(str(root),), max_depth=2, max_files=2)
    assert len(bounded.list_models()) == 2


# -- 4. no silent network access ---------------------------------------------


def test_network_is_disabled_by_default():
    config = RuntimeConfig.from_dict({}, env={})
    assert config.allow_network is False

    runtime = make_runtime()
    assert runtime.config.allow_network is False
    assert runtime.status()["runtime"]["config"]["allow_network"] is False


def test_ollama_backend_never_dials_when_network_is_disabled(monkeypatch):
    calls = []

    def _fake_urlopen(*args, **kwargs):
        calls.append(args)
        raise AssertionError("the runtime must not open a socket")

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    backend = OllamaBackend(allow_network=False)
    assert backend.available()[0] is False
    with pytest.raises(ModelRuntimeError):
        backend.list_models()
    with pytest.raises(ModelRuntimeError):
        backend.generate(RuntimeRequest(prompt="x",
                                                    model="llama3.2"))
    health = backend.health(probe=True)
    assert health.status == "unavailable"
    assert calls == []


def test_network_requires_an_explicit_opt_in(monkeypatch):
    reached = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"models": [{"name": "llama3.2", "size": 10}]}'

    def _fake_urlopen(request, timeout=None):
        reached.append((request.full_url, timeout))
        return _Response()

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    disabled = OllamaBackend(allow_network=False)
    with pytest.raises(ModelRuntimeError):
        disabled.list_models()
    assert reached == []

    enabled = OllamaBackend(allow_network=True)
    models = enabled.list_models()
    assert [model.name for model in models] == ["llama3.2"]
    assert reached and reached[0][0].endswith("/api/tags")


def test_runtime_config_reads_network_only_from_an_explicit_flag():
    assert RuntimeConfig.from_dict({}, env={}).allow_network is False
    assert RuntimeConfig.from_dict(
        {}, env={"FORGE_RUNTIME_ALLOW_NETWORK": "0"}).allow_network is False
    assert RuntimeConfig.from_dict(
        {}, env={"FORGE_RUNTIME_ALLOW_NETWORK": "1"}).allow_network is True


# -- 5. explicit backend selection -------------------------------------------


def test_no_implicit_failover_between_backends():
    good = ScriptedBackend(name="good", response="from-good")
    runtime = make_runtime(good, ExplodingBackend(name="boom"),
                           default_backend="boom")

    response = runtime.generate(RuntimeRequest(prompt="x"))
    assert response.success is False
    assert response.backend == "boom"
    assert good.generate_calls == []


def test_default_backend_is_only_the_configured_one():
    runtime = make_runtime(ScriptedBackend(name="alpha"),
                           ScriptedBackend(name="beta"),
                           default_backend="alpha")
    assert runtime.select_backend().name == "alpha"
    assert runtime.config.default_backend == "alpha"
