"""A81 hardening — regression tests for every defect found by audit.

Each test here pins one specific behaviour that was previously wrong, so a
regression fails loudly instead of quietly restoring the old behaviour. The
docstring on each test names the defect it guards.

As everywhere in this suite, the doubles replay caller-supplied text: a
passing test proves the *runtime* behaved, never that a model produced
anything.
"""
from __future__ import annotations

import json
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a81 import (  # noqa: E402
    ScriptedBackend, make_runtime, run_in_thread, wait_until,
)

from forge.runtime.model_runtime import (  # noqa: E402
    BackendKind, BackendProtocolError, BackendUnavailableError, ErrorKind,
    InferenceAdapter, ModelBackend, ModelNotFoundError, NativeBackend,
    OllamaBackend, RETRYABLE_ERROR_KINDS, RuntimeCapacityError, RuntimeChunk,
    RuntimeConfig, RuntimeHealth, RuntimeModel, RuntimeRequest,
    RuntimeResponse, RuntimeSecurityError, RuntimeState, describe_gguf,
    describe_safetensors,
)


# ---------------------------------------------------------------------------
# Artifact writers: real bytes, so the parsers are exercised for real
# ---------------------------------------------------------------------------


def write_gguf(path: Path, *, version: int = 3, arch: str = "llama",
               context_length: int = 0, embedding_length: int = 0,
               block_count: int = 0, kv_count: int = None) -> Path:
    """Write a structurally valid GGUF header plus a real key/value block."""
    entries = []
    if arch:
        entries.append(("general.architecture", 8, arch))
    if context_length:
        entries.append(("%s.context_length" % arch, 4, context_length))
    if embedding_length:
        entries.append(("%s.embedding_length" % arch, 4, embedding_length))
    if block_count:
        entries.append(("%s.block_count" % arch, 4, block_count))

    body = bytearray()
    for key, vtype, value in entries:
        encoded = key.encode("utf-8")
        body += struct.pack("<Q", len(encoded)) + encoded
        body += struct.pack("<I", vtype)
        if vtype == 8:  # string
            raw = str(value).encode("utf-8")
            body += struct.pack("<Q", len(raw)) + raw
        else:  # uint32
            body += struct.pack("<I", int(value))

    if kv_count is None:
        kv_count = len(entries)

    with open(str(path), "wb") as handle:
        handle.write(b"GGUF")
        if version == 1:
            # v1 stores 32-bit counts; v2/v3 store 64-bit counts.
            handle.write(struct.pack("<III", version, 0, kv_count))
        else:
            handle.write(struct.pack("<IQQ", version, 0, kv_count))
        handle.write(bytes(body))
        handle.write(b"\0" * 64)
    return path


def write_safetensors(path: Path, tensors) -> Path:
    """Write a structurally valid safetensors header with real shapes."""
    header = {"__metadata__": {"format": "pt"}}
    offset = 0
    for name, shape, dtype in tensors:
        count = 1
        for dim in shape:
            count *= dim
        width = 4 if dtype == "F32" else 2
        nbytes = count * width
        header[name] = {"dtype": dtype, "shape": list(shape),
                        "data_offsets": [offset, offset + nbytes]}
        offset += nbytes
    blob = json.dumps(header).encode("utf-8")
    with open(str(path), "wb") as handle:
        handle.write(struct.pack("<Q", len(blob)) + blob + b"\0" * offset)
    return path


# ---------------------------------------------------------------------------
# bug 1 — a producer thread must not outlive a failed stream
# ---------------------------------------------------------------------------


class MidStreamFailureAdapter(InferenceAdapter):
    """Emits one chunk, then breaks the protocol part-way through."""

    name = "midstream"

    def available(self):
        return (True, "test double")

    def generate(self, request, token=None):
        return RuntimeResponse(text="unused", success=True)

    def stream(self, request, token=None):
        yield RuntimeChunk(text="first")
        raise BackendProtocolError("mid-stream protocol failure")


def test_failed_stream_does_not_leak_its_producer_thread():
    """A stream that fails part-way must not leave its producer spinning."""
    runtime = make_runtime()
    runtime.register_backend(
        NativeBackend(adapter=MidStreamFailureAdapter()), replace=True)

    before = threading.active_count()
    with pytest.raises(BackendProtocolError):
        for _chunk in runtime.stream(
                RuntimeRequest(prompt="p", backend="native", timeout=5.0)):
            pass
    # The producer notices the consumer is gone on its next bounded put.
    assert wait_until(
        lambda: not [t for t in threading.enumerate()
                     if t.name == "forge-runtime-stream"], timeout=3.0)
    assert threading.active_count() <= before + 1
    runtime.close()


# ---------------------------------------------------------------------------
# bug 2 — nonsense timeouts are refused, not silently reinterpreted
# ---------------------------------------------------------------------------


def test_config_rejects_non_finite_timeouts():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            RuntimeConfig(timeout_seconds=bad).validate()
    with pytest.raises(ValueError):
        RuntimeConfig(chunk_timeout_seconds=float("nan")).validate()


# ---------------------------------------------------------------------------
# bug 3 — two artifacts with the same basename must both be discovered
# ---------------------------------------------------------------------------


def test_same_basename_in_different_dirs_are_both_discovered(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    write_gguf(first / "same.gguf", context_length=4096)
    write_gguf(second / "same.gguf", context_length=131072)

    backend = NativeBackend(model_dirs=[str(first), str(second)])
    models = backend.list_models()

    assert len(models) == 2, [m.model_id for m in models]
    assert len({m.model_id for m in models}) == 2
    # Neither artifact is silently dropped, and both keep their own metadata.
    assert sorted(m.context_window for m in models) == [4096, 131072]


def test_unique_basenames_keep_plain_filenames(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    write_gguf(root / "alpha.gguf", context_length=1024)
    write_gguf(root / "beta.gguf", context_length=2048)

    names = [m.name for m in NativeBackend(model_dirs=[str(root)]).list_models()]
    assert names == ["alpha.gguf", "beta.gguf"]


def test_same_basename_in_subdirectories_are_distinguished(tmp_path):
    root = tmp_path / "models"
    (root / "one").mkdir(parents=True)
    (root / "two").mkdir(parents=True)
    write_gguf(root / "one" / "dup.gguf", context_length=111)
    write_gguf(root / "two" / "dup.gguf", context_length=222)

    models = NativeBackend(model_dirs=[str(root)]).list_models()
    assert len(models) == 2
    assert sorted(m.name for m in models) == ["one/dup.gguf", "two/dup.gguf"]


# ---------------------------------------------------------------------------
# bug 4 — redaction input is bounded before the regexes run
# ---------------------------------------------------------------------------


def test_huge_error_message_is_truncated_before_redaction():
    from forge.runtime.model_runtime import MAX_ERROR_CHARS, _error_text

    huge = "x" * (5 * 1024 * 1024)
    started = time.perf_counter()
    result = _error_text(RuntimeError(huge))
    elapsed = time.perf_counter() - started

    assert len(result) <= MAX_ERROR_CHARS
    assert elapsed < 1.0, "redaction scanned the whole message: %.3fs" % elapsed


def test_secret_is_still_redacted_inside_a_large_message():
    from forge.runtime.model_runtime import _error_text

    padded = "noise " * 500
    result = _error_text(
        RuntimeError(padded + " api_key=SUPERSECRETVALUE123"))
    assert "SUPERSECRETVALUE123" not in result


# ---------------------------------------------------------------------------
# bug 5 — the fabric bridge must raise RuntimeError, never ModelRuntimeError
# ---------------------------------------------------------------------------


def test_runtime_provider_stream_wraps_failures_as_runtime_error():
    from forge.models import RuntimeProvider

    runtime = make_runtime(ScriptedBackend(name="alpha"))
    runtime.close()
    provider = RuntimeProvider(runtime, backend="alpha", model="alpha:x")

    with pytest.raises(RuntimeError):
        list(provider.stream("hello"))


# ---------------------------------------------------------------------------
# bug 6 — a non-iterable stream is a protocol violation, not a generic error
# ---------------------------------------------------------------------------


class NonIterableStreamBackend(ModelBackend):
    name = "noniterable"
    kind = BackendKind.CUSTOM.value
    local = True
    requires_network = False

    def available(self):
        return (True, "test double")

    def health(self, probe=True):
        return RuntimeHealth(backend=self.name, kind=self.kind,
                             status=RuntimeState.READY.value,
                             checked_at=time.time())

    def generate(self, request, token=None):
        return RuntimeResponse(text="ok", success=True)

    def stream(self, request, token=None):
        return 42  # deliberately not iterable


def test_non_iterable_stream_is_reported_as_a_protocol_error():
    runtime = make_runtime(NonIterableStreamBackend())
    with pytest.raises(BackendProtocolError) as excinfo:
        for _chunk in runtime.stream(
                RuntimeRequest(prompt="p", backend="noniterable", timeout=2.0)):
            pass
    assert excinfo.value.kind == ErrorKind.PROTOCOL
    assert "not iterable" in str(excinfo.value)
    runtime.close()


# ---------------------------------------------------------------------------
# bug 7 — loading a model with no artifact path says so honestly
# ---------------------------------------------------------------------------


def test_load_without_an_artifact_path_reports_the_real_problem(tmp_path):
    backend = NativeBackend(model_dirs=[str(tmp_path)])
    ghost = RuntimeModel(model_id="native:ghost", name="ghost",
                         backend="native", path="")

    with pytest.raises(ModelNotFoundError) as excinfo:
        backend.load_model(ghost)
    message = str(excinfo.value)
    assert "no artifact path" in message
    # The old behaviour blamed the model_dirs containment check instead.
    assert "outside the configured model directories" not in message


# ---------------------------------------------------------------------------
# bug 10 — explicit zeros in configuration are honoured, not swallowed
# ---------------------------------------------------------------------------


def test_explicit_zero_config_values_are_preserved():
    config = RuntimeConfig.from_dict(
        {"history_size": 0, "retries": 0, "retry_backoff_seconds": 0,
         "max_resident_bytes": 0}, env={})
    assert config.history_size == 0
    assert config.retries == 0
    assert config.retry_backoff_seconds == 0.0
    assert config.max_resident_bytes == 0
    # Untouched fields still fall back to their defaults.
    assert config.timeout_seconds == 120.0


def test_genuinely_invalid_zero_timeouts_are_rejected_not_coerced():
    for key in ("timeout_seconds", "chunk_timeout_seconds",
                "health_timeout_seconds"):
        with pytest.raises(ValueError):
            RuntimeConfig.from_dict({key: 0}, env={})


def test_malformed_config_numbers_are_rejected():
    with pytest.raises(ValueError) as excinfo:
        RuntimeConfig.from_dict({"timeout_seconds": "soon"}, env={})
    assert "soon" in str(excinfo.value)


# ---------------------------------------------------------------------------
# bug 11 — an Ollama transport failure mid-stream is classified, not raw
# ---------------------------------------------------------------------------


class ResetStreamResponse:
    def __iter__(self):
        raise OSError("connection reset by peer")

    def close(self):
        pass


def test_ollama_stream_transport_failure_becomes_backend_unavailable():
    backend = OllamaBackend(base_url="http://127.0.0.1:11434",
                            allow_network=True, default_model="m",
                            health_timeout=1.0)
    backend._stream_request = lambda path, payload, timeout=None: (
        ResetStreamResponse())

    with pytest.raises(BackendUnavailableError) as excinfo:
        for _chunk in backend.stream(
                RuntimeRequest(prompt="p", model="m", timeout=1.0)):
            pass
    assert excinfo.value.kind == ErrorKind.UNAVAILABLE
    assert "connection reset" in str(excinfo.value)


# ---------------------------------------------------------------------------
# bug 12 — cancellation keeps real output and surfaces immediately
# ---------------------------------------------------------------------------


class LatePayloadAdapter(InferenceAdapter):
    """Produces its text only after the consumer has already given up."""

    name = "late"

    def __init__(self) -> None:
        self.release = threading.Event()

    def available(self):
        return (True, "test double")

    def generate(self, request, token=None):
        return RuntimeResponse(text="unused", success=True)

    def stream(self, request, token=None):
        # Ignores the token entirely: the runtime must still cope.
        self.release.wait(3.0)
        yield RuntimeChunk(text="valuable", request_id=request.request_id)


def test_cancelled_stream_reports_what_the_backend_really_produced():
    adapter = LatePayloadAdapter()
    runtime = make_runtime()
    runtime.register_backend(NativeBackend(adapter=adapter), replace=True)

    stream = runtime.stream(
        RuntimeRequest(prompt="p", backend="native", timeout=10.0))
    outcome = {}

    def consume():
        try:
            for _chunk in stream:
                pass
        except BaseException as exc:  # noqa: BLE001 - recorded for assertion
            outcome["error"] = type(exc).__name__

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()
    time.sleep(0.2)

    started = time.perf_counter()
    assert stream.cancel("user") is True
    adapter.release.set()
    thread.join(5.0)
    elapsed = time.perf_counter() - started

    response = stream.response
    assert outcome.get("error") == "RuntimeCancelledError"
    # Cancellation is noticed promptly rather than after the chunk bound.
    assert elapsed < 2.0, "cancellation took %.2fs" % elapsed
    assert response.cancelled is True
    assert response.timed_out is False
    assert response.success is False
    assert response.finish_reason == "cancelled"
    # The real output is kept and labelled partial, not dropped or completed.
    assert response.text == "valuable"
    assert response.metadata.get("partial") is True
    runtime.close()


def test_stream_cancellation_does_not_wait_for_the_chunk_bound():
    """The consumer must not park for chunk_timeout after a cancellation."""
    from helpers_a81 import HangingBackend

    runtime = make_runtime(HangingBackend(name="hang", hold=30.0),
                           default_backend="hang", chunk_timeout_seconds=30.0,
                           timeout_seconds=30.0, max_timeout_seconds=30.0)
    stream = runtime.stream(
        RuntimeRequest(prompt="p", backend="hang", timeout=30.0))
    outcome = {}

    def consume():
        try:
            for _chunk in stream:
                pass
        except BaseException as exc:  # noqa: BLE001 - recorded for assertion
            outcome["error"] = type(exc).__name__

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()
    time.sleep(0.2)
    started = time.perf_counter()
    stream.cancel("user")
    thread.join(5.0)
    elapsed = time.perf_counter() - started

    assert not thread.is_alive(), "consumer stayed parked in the queue wait"
    assert elapsed < 2.0, "cancellation took %.2fs" % elapsed
    assert stream.response.cancelled is True
    assert stream.response.timed_out is False
    runtime.close()


# ---------------------------------------------------------------------------
# bug 13 — a broken config file is never silently ignored
# ---------------------------------------------------------------------------


def test_malformed_config_file_is_reported_not_ignored(tmp_path):
    broken = tmp_path / "runtime.json"
    broken.write_text('{"default_backend": "ollama", ', encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        RuntimeConfig.load(str(broken))
    message = str(excinfo.value)
    assert "runtime.json" in message
    assert "JSON" in message


def test_malformed_yaml_config_is_reported_not_ignored(tmp_path):
    broken = tmp_path / "runtime.yaml"
    broken.write_text("default_backend: [unclosed\n  - : :\n", encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        RuntimeConfig.load(str(broken))
    assert "runtime.yaml" in str(excinfo.value)


def test_non_mapping_config_is_rejected(tmp_path):
    scalar = tmp_path / "runtime.json"
    scalar.write_text("42", encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        RuntimeConfig.load(str(scalar))
    assert "mapping" in str(excinfo.value)


def test_valid_config_is_still_honoured(tmp_path):
    good = tmp_path / "runtime.json"
    good.write_text(json.dumps(
        {"default_backend": "ollama", "history_size": 7, "retries": 2}),
        encoding="utf-8")

    config = RuntimeConfig.load(str(good))
    assert config.default_backend == "ollama"
    assert config.history_size == 7
    assert config.retries == 2


def test_missing_config_file_is_still_not_an_error(tmp_path):
    config = RuntimeConfig.load(str(tmp_path / "absent.json"))
    assert config.default_backend == "native"
    assert config.allow_network is False


# ---------------------------------------------------------------------------
# bug 15 — missing model directories are surfaced, not silently dropped
# ---------------------------------------------------------------------------


def test_missing_model_dirs_are_reported(tmp_path):
    missing = tmp_path / "does-not-exist"
    backend = NativeBackend(model_dirs=[str(missing)])

    assert list(backend.invalid_dirs()) == [str(missing)]

    health = backend.health(probe=True)
    assert str(missing) in health.detail
    assert "Missing or unreadable model_dirs" in health.detail

    resources = backend.resources()
    assert resources["invalid_model_dirs"] == [str(missing)]
    assert list(backend.list_models()) == []


def test_a_ready_backend_with_missing_dirs_is_degraded_not_ready(tmp_path):
    """Discovery cannot work, so 'ready' would be a false claim."""

    class ReadyAdapter(InferenceAdapter):
        name = "ready"

        def available(self):
            return (True, "test double")

        def generate(self, request, token=None):
            return RuntimeResponse(text="ok", success=True)

    backend = NativeBackend(model_dirs=[str(tmp_path / "absent")],
                            adapter=ReadyAdapter())
    assert backend.health(probe=True).status == RuntimeState.DEGRADED.value


def test_valid_dirs_are_not_reported_as_invalid(tmp_path):
    (tmp_path / "models").mkdir()
    backend = NativeBackend(model_dirs=[str(tmp_path / "models")])
    assert backend.invalid_dirs() == ()


# ---------------------------------------------------------------------------
# bug 16 + power A — GGUF metadata is really parsed
# ---------------------------------------------------------------------------


def test_gguf_key_value_metadata_is_parsed(tmp_path):
    path = write_gguf(tmp_path / "model.gguf", arch="llama",
                      context_length=131072, embedding_length=2048,
                      block_count=28)
    described = describe_gguf(str(path))

    assert described["header_ok"] is True
    assert described["gguf_version"] == 3
    assert described["architecture"] == "llama"
    assert described["context_window"] == 131072
    assert described["embedding_length"] == 2048
    assert described["block_count"] == 28
    assert described["metadata_parsed"] == 4
    assert described["metadata_truncated"] is False


def test_gguf_version_one_header_is_not_misread(tmp_path):
    """v1 stores 32-bit counts; reading it as v2/v3 yields garbage."""
    path = write_gguf(tmp_path / "v1.gguf", version=1)
    described = describe_gguf(str(path))
    assert described["header_ok"] is True
    assert described["gguf_version"] == 1


def test_gguf_arrays_are_counted_without_being_materialised(tmp_path):
    """A real array is skipped over, not copied into the reported metadata."""
    path = tmp_path / "arrays.gguf"
    body = bytearray()

    def put_string(target, key, value):
        raw_key = key.encode("utf-8")
        target += struct.pack("<Q", len(raw_key)) + raw_key
        target += struct.pack("<I", 8)
        raw = value.encode("utf-8")
        target += struct.pack("<Q", len(raw)) + raw

    put_string(body, "general.architecture", "llama")
    # A real, fully-present array of 4 strings, followed by one more scalar
    # entry: reaching that entry proves the array was skipped correctly.
    key = b"tokenizer.ggml.tokens"
    body += struct.pack("<Q", len(key)) + key
    body += struct.pack("<I", 9)  # array
    body += struct.pack("<I", 8)  # of strings
    body += struct.pack("<Q", 4)  # element count, and they are all present
    for index in range(4):
        token = ("tok%d" % index).encode("utf-8")
        body += struct.pack("<Q", len(token)) + token
    put_string(body, "general.name", "test-model")

    with open(str(path), "wb") as handle:
        handle.write(b"GGUF" + struct.pack("<IQQ", 3, 0, 3))
        handle.write(bytes(body))
        handle.write(b"\0" * 32)

    described = describe_gguf(str(path))
    assert described["header_ok"] is True
    assert described["metadata_entries"] == 3
    assert described["metadata_parsed"] == 3
    assert described["metadata_truncated"] is False
    # The array is counted and skipped: its contents are not reported, while
    # the entry that follows it is — proving the reader advanced correctly.
    assert described["architecture"] == "llama"
    assert described["general"] == {"general.architecture": "llama",
                                    "general.name": "test-model"}


def test_gguf_array_over_the_element_cap_is_refused_not_read(tmp_path):
    """A hostile element count must be refused, not looped over."""
    from forge.runtime.model_runtime import GGUF_MAX_ARRAY_ELEMENTS

    path = tmp_path / "hostile.gguf"
    body = bytearray()
    key = b"tokenizer.ggml.tokens"
    body += struct.pack("<Q", len(key)) + key
    body += struct.pack("<I", 9)  # array
    body += struct.pack("<I", 8)  # of strings
    body += struct.pack("<Q", GGUF_MAX_ARRAY_ELEMENTS + 1)

    with open(str(path), "wb") as handle:
        handle.write(b"GGUF" + struct.pack("<IQQ", 3, 0, 1))
        handle.write(bytes(body))
        handle.write(b"\0" * 32)

    started = time.perf_counter()
    described = describe_gguf(str(path))
    elapsed = time.perf_counter() - started

    assert described["metadata_truncated"] is True
    assert described["metadata_parsed"] == 0
    assert elapsed < 2.0, "refusal took %.2fs" % elapsed


def test_gguf_entry_count_over_the_cap_is_bounded(tmp_path):
    """A file declaring a huge KV count stops at the cap and says so."""
    from forge.runtime.model_runtime import GGUF_MAX_KV_ENTRIES

    declared = GGUF_MAX_KV_ENTRIES + 10
    path = tmp_path / "many.gguf"
    body = bytearray()
    for index in range(declared):
        key = ("k%d" % index).encode("utf-8")
        body += struct.pack("<Q", len(key)) + key
        body += struct.pack("<I", 4)  # uint32
        body += struct.pack("<I", index)

    with open(str(path), "wb") as handle:
        handle.write(b"GGUF" + struct.pack("<IQQ", 3, 0, declared))
        handle.write(bytes(body))

    described = describe_gguf(str(path))
    assert described["metadata_entries"] == declared
    assert described["metadata_parsed"] == GGUF_MAX_KV_ENTRIES
    assert described["metadata_truncated"] is True


def test_gguf_respects_the_scan_bound(tmp_path):
    """Metadata beyond the scanned window is not read, and is flagged."""
    path = write_gguf(tmp_path / "model.gguf", arch="llama",
                      context_length=32768, embedding_length=4096,
                      block_count=32)

    # A window large enough for the whole key/value section parses cleanly.
    full = describe_gguf(str(path))
    assert full["metadata_parsed"] == 4
    assert full["metadata_truncated"] is False
    assert full["context_window"] == 32768

    # A window that cuts the section short stops early and says so, rather
    # than silently reporting a partial read as complete.
    narrow = describe_gguf(str(path), scan_bytes=64)
    assert narrow["header_ok"] is True
    assert narrow["metadata_parsed"] < 4
    assert narrow["metadata_truncated"] is True


def test_gguf_read_bound_is_enforced_by_the_reader(tmp_path):
    """The scan never reads more of the file than the configured bound."""
    from forge.runtime.model_runtime import _read_header

    path = tmp_path / "big.gguf"
    path.write_bytes(b"GGUF" + b"\0" * 10000)
    assert len(_read_header(str(path), 512)) == 512
    assert len(_read_header(str(path), 100000)) == 10004
    assert _read_header(str(tmp_path / "absent.gguf"), 512) == b""


def test_gguf_truncated_metadata_is_flagged(tmp_path):
    """A header claiming more entries than exist must be reported, not faked."""
    path = write_gguf(tmp_path / "lying.gguf", context_length=1024,
                      kv_count=99)
    described = describe_gguf(str(path))
    assert described["metadata_truncated"] is True
    assert described["metadata_parsed"] < 99


# ---------------------------------------------------------------------------
# power B — safetensors parameters and dtypes
# ---------------------------------------------------------------------------


def test_safetensors_parameter_count_and_dtypes(tmp_path):
    path = write_safetensors(tmp_path / "model.safetensors", [
        ("weight.a", (10, 20), "F32"),
        ("weight.b", (5, 5, 2), "F16"),
    ])
    described = describe_safetensors(str(path))

    assert described["header_ok"] is True
    assert described["tensor_count"] == 2
    assert described["parameter_count"] == 10 * 20 + 5 * 5 * 2
    assert sorted(described["dtypes"]) == ["F16", "F32"]


def test_discovered_model_carries_parsed_metadata(tmp_path):
    write_gguf(tmp_path / "llama.gguf", context_length=32768, arch="mistral")
    model = NativeBackend(model_dirs=[str(tmp_path)]).list_models()[0]

    assert model.context_window == 32768
    assert model.kind == "mistral"
    assert model.metadata["metadata_parsed"] >= 2
    assert model.metadata["source"] == "native-discovery"


# ---------------------------------------------------------------------------
# power C — bounded retries on the same backend only
# ---------------------------------------------------------------------------


class FlakyBackend(ModelBackend):
    """Fails a fixed number of times, then replays caller-supplied text."""

    kind = BackendKind.CUSTOM.value
    local = True
    requires_network = False

    def __init__(self, name, fail_times, error=None):
        self.name = name
        self.fail_times = fail_times
        self.error = error or BackendUnavailableError("transient blip")
        self.calls = 0

    def available(self):
        return (True, "test double")

    def health(self, probe=True):
        return RuntimeHealth(backend=self.name, kind=self.kind,
                             status=RuntimeState.READY.value,
                             checked_at=time.time())

    def generate(self, request, token=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error
        return RuntimeResponse(text="recovered-%d" % self.calls, success=True)


def test_retry_recovers_from_a_transient_failure():
    backend = FlakyBackend("flaky", fail_times=2)
    runtime = make_runtime(backend, retries=3, retry_backoff_seconds=0.005)

    response = runtime.generate(
        RuntimeRequest(prompt="p", backend="flaky", timeout=10.0))
    assert response.success is True
    assert response.text == "recovered-3"
    assert response.metadata["attempts"] == 3
    assert backend.calls == 3
    runtime.close()


def test_retries_are_bounded_by_the_configured_count():
    backend = FlakyBackend("flaky", fail_times=99)
    runtime = make_runtime(backend, retries=2, retry_backoff_seconds=0.005)

    response = runtime.generate(
        RuntimeRequest(prompt="p", backend="flaky", timeout=10.0))
    assert response.success is False
    assert response.error_kind == "unavailable"
    assert response.metadata["attempts"] == 3
    assert backend.calls == 3
    runtime.close()


def test_retries_default_to_zero():
    backend = FlakyBackend("flaky", fail_times=1)
    runtime = make_runtime(backend, retry_backoff_seconds=0.005)

    response = runtime.generate(
        RuntimeRequest(prompt="p", backend="flaky", timeout=10.0))
    assert response.success is False
    assert response.metadata["attempts"] == 1
    assert backend.calls == 1
    runtime.close()


def test_a_per_request_retry_override_is_honoured():
    backend = FlakyBackend("flaky", fail_times=2)
    runtime = make_runtime(backend, retries=0, retry_backoff_seconds=0.005)

    response = runtime.generate(
        RuntimeRequest(prompt="p", backend="flaky", timeout=10.0, retries=4))
    assert response.success is True
    assert response.metadata["attempts"] == 3
    runtime.close()


@pytest.mark.parametrize("error", [
    RuntimeSecurityError("refused"),
    ModelNotFoundError("no such model"),
    RuntimeCapacityError("over the cap"),
])
def test_non_retryable_failures_are_never_retried(error):
    backend = FlakyBackend("flaky", fail_times=99, error=error)
    runtime = make_runtime(backend, retries=3, retry_backoff_seconds=0.005)

    response = runtime.generate(
        RuntimeRequest(prompt="p", backend="flaky", timeout=10.0))
    assert response.success is False
    assert response.metadata["attempts"] == 1
    assert backend.calls == 1
    runtime.close()


def test_retries_never_cross_backends():
    """A retry must not silently move to a different backend."""
    flaky = FlakyBackend("flaky", fail_times=99)
    other = ScriptedBackend(name="other", response="from-other")
    runtime = make_runtime(flaky, other, retries=2,
                           retry_backoff_seconds=0.005)

    response = runtime.generate(
        RuntimeRequest(prompt="p", backend="flaky", timeout=10.0))
    assert response.success is False
    assert response.backend == "flaky"
    assert response.text == ""
    assert other.generate_calls == []
    runtime.close()


def test_retryable_kinds_are_the_transient_ones():
    assert RETRYABLE_ERROR_KINDS == frozenset(
        {ErrorKind.BACKEND.value, ErrorKind.UNAVAILABLE.value,
         ErrorKind.PROTOCOL.value})
    for kind in (ErrorKind.CANCELLED, ErrorKind.TIMEOUT, ErrorKind.SECURITY,
                 ErrorKind.NOT_FOUND, ErrorKind.CONFLICT, ErrorKind.CLOSED):
        assert kind.value not in RETRYABLE_ERROR_KINDS


# ---------------------------------------------------------------------------
# power D — metrics are computed from real outcomes
# ---------------------------------------------------------------------------


def test_metrics_report_percentiles_and_success_rate():
    alpha = ScriptedBackend(name="alpha", response="a")
    beta = FlakyBackend("beta", fail_times=99)
    runtime = make_runtime(alpha, beta, retry_backoff_seconds=0.005)

    for _index in range(4):
        runtime.generate(RuntimeRequest(prompt="p", backend="alpha",
                                        timeout=5.0))
    runtime.generate(RuntimeRequest(prompt="p", backend="beta", timeout=5.0))

    metrics = runtime.metrics()
    assert metrics["requests"] == 5
    assert metrics["successes"] == 4
    assert metrics["failures"] == 1
    assert metrics["success_rate"] == 0.8
    for key in ("p50", "p95", "p99", "min", "max"):
        assert metrics["latency_ms"][key] is not None
    assert metrics["error_kinds"] == {"unavailable": 1}
    assert set(metrics["by_backend"]) >= {"alpha", "beta"}
    assert metrics["by_backend"]["alpha"]["success_rate"] == 1.0
    assert metrics["by_backend"]["beta"]["success_rate"] == 0.0
    runtime.close()


def test_metrics_on_an_empty_window_report_none_not_zero():
    """A fabricated 0.0 would read as 'instant'; None says 'no data'."""
    runtime = make_runtime(ScriptedBackend(name="alpha"))
    metrics = runtime.metrics()
    assert metrics["requests"] == 0
    assert metrics["success_rate"] is None
    assert metrics["latency_ms"]["p50"] is None
    runtime.close()


def test_metrics_can_be_scoped_to_one_backend():
    alpha = ScriptedBackend(name="alpha", response="a")
    beta = ScriptedBackend(name="beta", response="b")
    runtime = make_runtime(alpha, beta)

    runtime.generate(RuntimeRequest(prompt="p", backend="alpha", timeout=5.0))
    runtime.generate(RuntimeRequest(prompt="p", backend="beta", timeout=5.0))
    runtime.generate(RuntimeRequest(prompt="p", backend="beta", timeout=5.0))

    scoped = runtime.metrics("beta")
    assert scoped["requests"] == 2
    assert scoped["backend"] == "beta"
    assert scoped["successes"] == 2
    runtime.close()


def test_metrics_record_retry_attempts():
    backend = FlakyBackend("flaky", fail_times=2)
    runtime = make_runtime(backend, retries=3, retry_backoff_seconds=0.005)
    runtime.generate(RuntimeRequest(prompt="p", backend="flaky", timeout=10.0))

    metrics = runtime.metrics()
    assert metrics["requests"] == 1
    assert metrics["successes"] == 1
    assert metrics["retried_requests"] == 1
    assert metrics["retry_attempts"] == 3
    runtime.close()


def test_metrics_contain_no_prompt_or_completion_text():
    secret_prompt = "PROMPT-TEXT-THAT-MUST-NOT-APPEAR"
    runtime = make_runtime(ScriptedBackend(name="alpha",
                                           response=secret_prompt))
    runtime.generate(
        RuntimeRequest(prompt=secret_prompt, backend="alpha", timeout=5.0))

    blob = json.dumps(runtime.metrics())
    assert secret_prompt not in blob
    assert json.dumps(runtime.history()) .find(secret_prompt) == -1
    runtime.close()


# ---------------------------------------------------------------------------
# refusals are visible, without inventing a backend that does not exist
# ---------------------------------------------------------------------------


def test_unknown_backend_refusal_is_logged_but_not_counted():
    runtime = make_runtime(ScriptedBackend(name="alpha"))
    response = runtime.generate(RuntimeRequest(prompt="p", backend="boom"))

    assert response.success is False
    assert response.error_kind == "not_found"
    # A backend that was never registered gets no lifetime counter entry.
    assert "boom" not in runtime.status()["counters"]
    # ...but the refusal is still visible rather than silently dropped.
    refusals = runtime.refusals()
    assert len(refusals) == 1
    assert refusals[0]["backend"] == "boom"
    assert refusals[0]["error_kind"] == "not_found"
    # It never reached a backend, so it is not a backend outcome.
    assert [entry["backend"] for entry in runtime.history()] == []
    assert runtime.metrics()["refusals"] == {"not_found": 1}
    runtime.close()


def test_a_conflict_refusal_does_not_evict_the_in_flight_request():
    """A duplicate id must not make the original request uncancellable."""
    from helpers_a81 import HangingBackend

    runtime = make_runtime(HangingBackend(name="hang", hold=30.0),
                           default_backend="hang", timeout_seconds=30.0,
                           max_timeout_seconds=30.0)
    thread, results = run_in_thread(
        runtime.generate,
        RuntimeRequest(prompt="p", backend="hang", request_id="dup-id",
                       timeout=30.0))
    assert wait_until(lambda: runtime.in_flight(), timeout=3.0)

    clash = runtime.generate(
        RuntimeRequest(prompt="p", backend="hang", request_id="dup-id",
                       timeout=1.0))
    assert clash.error_kind == "conflict"
    assert runtime.metrics()["refusals"] == {"conflict": 1}

    # The original request is still in flight and still cancellable.
    assert [entry["request_id"] for entry in runtime.in_flight()] == ["dup-id"]
    assert runtime.cancel("dup-id") is True
    thread.join(5.0)
    assert results and results[0].cancelled is True
    runtime.close()


# ---------------------------------------------------------------------------
# power E — the resident-memory cap is enforced
# ---------------------------------------------------------------------------


def test_resident_cap_refuses_an_oversized_load(tmp_path):
    write_gguf(tmp_path / "big.gguf", context_length=1024)
    backend = NativeBackend(model_dirs=[str(tmp_path)], max_resident_bytes=1)
    model = backend.list_models()[0]

    with pytest.raises(RuntimeCapacityError) as excinfo:
        backend.load_model(model)
    message = str(excinfo.value)
    assert excinfo.value.kind == ErrorKind.UNAVAILABLE
    assert "max_resident_bytes" in message
    assert backend.loaded_models() == []


def test_resident_cap_of_zero_means_unbounded(tmp_path):
    write_gguf(tmp_path / "model.gguf", context_length=1024)
    backend = NativeBackend(model_dirs=[str(tmp_path)])
    assert backend.max_resident_bytes == 0
    assert backend.load_model(backend.list_models()[0]).loaded is True
    assert backend.resident_bytes() > 0


def test_resident_cap_accounts_for_already_loaded_models(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    write_gguf(first / "one.gguf", context_length=1024)
    write_gguf(second / "two.gguf", context_length=2048)

    backend = NativeBackend(model_dirs=[str(first), str(second)])
    models = backend.list_models()
    size = models[0].size_bytes
    # Room for exactly one artifact, so the second load must be refused.
    backend.max_resident_bytes = size
    backend.load_model(models[0])
    assert backend.resident_bytes() == size

    with pytest.raises(RuntimeCapacityError):
        backend.load_model(models[1])


def test_capacity_cap_flows_from_config(tmp_path):
    config = RuntimeConfig.from_dict(
        {"model_dirs": str(tmp_path), "max_resident_bytes": 1}, env={})
    assert config.max_resident_bytes == 1

    from forge.runtime.model_runtime import create_backend
    backend = create_backend("native", config)
    assert backend.max_resident_bytes == 1


def test_capacity_refusal_is_not_retried(tmp_path):
    write_gguf(tmp_path / "model.gguf", context_length=1024)

    class CappedBackend(NativeBackend):
        name = "capped"
        kind = BackendKind.CUSTOM.value

        def __init__(self, model_dirs):
            NativeBackend.__init__(self, model_dirs=model_dirs,
                                   max_resident_bytes=1)
            self.calls = 0

        def available(self):
            return (True, "test double")

        def health(self, probe=True):
            return RuntimeHealth(backend=self.name, kind=self.kind,
                                 status=RuntimeState.READY.value,
                                 checked_at=time.time())

        def generate(self, request, token=None):
            self.calls += 1
            raise RuntimeCapacityError("over the resident cap")

    backend = CappedBackend([str(tmp_path)])
    runtime = make_runtime(backend, retries=3, retry_backoff_seconds=0.005)
    response = runtime.generate(
        RuntimeRequest(prompt="p", backend="capped", timeout=10.0))

    assert response.success is False
    assert response.error_kind == "unavailable"
    assert backend.calls == 1
    runtime.close()


# ---------------------------------------------------------------------------
# the native adapter must not swallow output on cancellation
# ---------------------------------------------------------------------------


def test_native_stream_forwards_the_chunk_it_already_received():
    """Dropping a received chunk would report text the backend did produce."""
    adapter = LatePayloadAdapter()
    backend = NativeBackend(adapter=adapter)
    token = __import__("forge.runtime.model_runtime",
                       fromlist=["CancellationToken"]).CancellationToken()

    thread = threading.Thread(target=token.cancel, args=("user",), daemon=True)
    chunks = []

    def consume():
        for chunk in backend.stream(
                RuntimeRequest(prompt="p"), token):
            chunks.append(chunk.text)

    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    time.sleep(0.1)
    thread.start()
    adapter.release.set()
    consumer.join(5.0)

    assert chunks == ["valuable"]
